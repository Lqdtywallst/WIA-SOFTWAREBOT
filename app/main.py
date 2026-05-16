from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.history_utils import read_jsonl_tail
from app.ig_client import IGClientError, IGMarketsClient
from app.logging_config import DecisionLogger, configure_logging
from app.models import PnLUpdate, TradingViewAlert
from app.paper_broker import PaperBroker
from app.risk_manager import RiskManager
from app.signal_validator import evaluate_signal

settings = get_settings()
logger = configure_logging(settings.logs_dir, settings.log_level)
decision_logger = DecisionLogger(settings.logs_dir)
risk_manager = RiskManager(settings)
ig_client: Optional[IGMarketsClient] = None
paper_broker = PaperBroker(settings.logs_dir)


def _enrich_paper_trades_for_pnl(
    trades: list[dict[str, Any]], settings: Settings
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach USD risk/reward scenarios using fixed fractional risk (aligned with position sizing)."""
    risk_usd = round(settings.account_equity * settings.risk_per_trade, 2)
    enriched: list[dict[str, Any]] = []
    sum_tp_open = 0.0
    sum_sl_open = 0.0
    open_n = 0
    for raw in trades:
        row = dict(raw)
        stop_d = float(row.get("stop_distance") or settings.default_stop_distance)
        tp_d = float(row.get("take_profit_distance") or settings.default_take_profit_distance)
        status = str(row.get("status") or "open").lower()
        if stop_d > 0:
            reward_risk = tp_d / stop_d
            pnl_sl = round(-risk_usd, 2)
            pnl_tp = round(reward_risk * risk_usd, 2)
        else:
            pnl_sl = None
            pnl_tp = None
        row["risk_usd"] = risk_usd
        row["pnl_if_sl_usd"] = pnl_sl
        row["pnl_if_tp_usd"] = pnl_tp
        if status == "open":
            open_n += 1
            if pnl_tp is not None:
                sum_tp_open += pnl_tp
            if pnl_sl is not None:
                sum_sl_open += pnl_sl
        enriched.append(row)
    summary = {
        "open_positions": open_n,
        "theoretical_tp_usd_open": round(sum_tp_open, 2),
        "theoretical_sl_usd_open": round(sum_sl_open, 2),
        "risk_per_trade_usd": risk_usd,
    }
    return enriched, summary


def _get_ig_client() -> IGMarketsClient:
    """IG HTTP client is only created when demo/live paths place real orders (paper mode skips IG entirely)."""
    global ig_client
    if ig_client is None:
        ig_client = IGMarketsClient(settings)
    return ig_client


app = FastAPI(title=settings.app_name)


@app.on_event("shutdown")
async def shutdown() -> None:
    global ig_client
    if ig_client is not None:
        await ig_client.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "trading_mode": settings.trading_mode,
        "ig_account_type": settings.ig_account_type,
        "live_trading_enabled": settings.live_trading_enabled,
        "dashboard_url": "/dashboard",
        "signal_filters": {
            "max_dist_vwap_atr": settings.max_dist_vwap_atr,
            "min_long_score": settings.min_long_score,
            "min_short_score": settings.min_short_score,
            "reject_on_rango_state": settings.reject_on_rango_state,
            "reject_on_no_chase_state": settings.reject_on_no_chase_state,
        },
        "symbol_map_size": len(settings.symbol_map),
        "ig_session_initialized": ig_client is not None,
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return _dashboard_html()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return _dashboard_html()


@app.get("/paper/trades")
async def paper_trades() -> dict[str, Any]:
    raw = paper_broker.list_trades()
    trades, pnl_summary = _enrich_paper_trades_for_pnl(raw, settings)
    return {"trades": trades, "risk": risk_manager.snapshot(), "pnl_summary": pnl_summary}


@app.get("/history/paper-trades")
async def history_paper_trades(limit: int = Query(100, ge=1, le=2000)) -> dict[str, Any]:
    path = settings.logs_dir / "paper_trades.jsonl"
    return {"source": str(path.name), "count_requested": limit, "rows": read_jsonl_tail(path, limit)}


@app.get("/history/decisions")
async def history_decisions(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    path = settings.logs_dir / "decisions.jsonl"
    return {"source": str(path.name), "count_requested": limit, "rows": read_jsonl_tail(path, limit)}


@app.get("/history/alerts")
async def history_alerts(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    path = settings.logs_dir / "alerts.jsonl"
    return {"source": str(path.name), "count_requested": limit, "rows": read_jsonl_tail(path, limit)}


@app.get("/risk")
async def risk_status() -> dict[str, object]:
    return risk_manager.snapshot()


@app.post("/risk/close")
async def close_trade(update: PnLUpdate) -> dict[str, object]:
    risk_manager.close_trade(update.symbol, update.realized_pnl)
    logger.info("Recorded closed trade for %s with PnL %.2f", update.symbol, update.realized_pnl)
    return risk_manager.snapshot()


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, Any]:
    payload = await _read_json(request)
    redacted_payload = _redact_secret(payload)
    decision_logger.log_alert(redacted_payload)
    logger.info("Received alert: %s", redacted_payload)

    alert = _parse_alert(payload)
    _validate_secret(alert)
    alert = _apply_symbol_map(alert)

    signal_decision = evaluate_signal(alert, settings)
    decision_record: dict[str, Any] = {
        "symbol": alert.symbol,
        "side": alert.side,
        "signal_allowed": signal_decision.allowed,
        "signal_reason": signal_decision.reason,
    }

    can_trade, risk_reason, order = risk_manager.build_order(signal_decision)
    decision_record.update({"risk_allowed": can_trade, "risk_reason": risk_reason})

    if not can_trade or order is None:
        decision_logger.log_decision(decision_record)
        return {"accepted": False, "reason": risk_reason, "risk": risk_manager.snapshot()}

    if settings.trading_mode == "paper":
        order_result = await paper_broker.place_market_order(order, alert)
    else:
        try:
            order_result = await _get_ig_client().place_market_order(order)
        except IGClientError as exc:
            decision_record.update({"order_submitted": False, "order_error": str(exc)})
            decision_logger.log_decision(decision_record)
            logger.exception("Order failed for %s", alert.symbol)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"IG order failed: {exc}",
            ) from exc

    if order_result.submitted:
        risk_manager.register_open_trade(order.symbol)

    decision_record.update(
        {
            "order_submitted": order_result.submitted,
            "order_mode": order_result.mode,
            "deal_reference": order_result.deal_reference,
            "order_reason": order_result.reason,
        }
    )
    decision_logger.log_decision(decision_record)

    return {
        "accepted": order_result.submitted,
        "reason": order_result.reason or "order submitted",
        "deal_reference": order_result.deal_reference,
        "mode": order_result.mode,
        "risk": risk_manager.snapshot(),
    }


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Alert body must be a JSON object",
        )
    return payload


def _parse_alert(payload: dict[str, Any]) -> TradingViewAlert:
    try:
        return TradingViewAlert.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc


def _apply_symbol_map(alert: TradingViewAlert) -> TradingViewAlert:
    key = alert.symbol.strip().upper()
    mapped = settings.symbol_map.get(key)
    if mapped is None:
        return alert
    return alert.model_copy(update={"symbol": mapped})


def _validate_secret(alert: TradingViewAlert) -> None:
    expected = settings.webhook_secret
    if expected is None:
        return

    if alert.secret != expected.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")


def _redact_secret(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = payload.copy()
    if "secret" in redacted:
        redacted["secret"] = "***"
    return redacted


def _dashboard_html() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="WIA-Software execution console — TradingView alerts, risk engine, paper simulation.">
  <title>WIA-Software · Execution Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #000000;
      --bg-elevated: #050505;
      --surface: #0a0a0a;
      --surface-muted: #111111;
      --border: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.14);
      --text: #ffffff;
      --muted: rgba(255, 255, 255, 0.52);
      --neon: #c084fc;
      --neon-bright: #e879f9;
      --neon-dim: rgba(192, 132, 252, 0.38);
      --purple-deep: #a855f7;
      --fluor: #39ff14;
      --fluor-bright: #7fff00;
      --fluor-dim: rgba(57, 255, 20, 0.32);
      --fluor-glow: rgba(57, 255, 20, 0.55);
      --accent-subtle: rgba(192, 132, 252, 0.55);
      --glow: rgba(192, 132, 252, 0.55);
      --glow-strong: rgba(168, 85, 247, 0.75);
      --long: #39ff14;
      --short: #f0abfc;
      --danger: #ff4d6d;
      --radius: 2px;
      --radius-sm: 2px;
      --font-sans: "IBM Plex Sans", system-ui, -apple-system, sans-serif;
      --font-mono: "IBM Plex Mono", ui-monospace, monospace;
      --ease: cubic-bezier(0.25, 0.1, 0.25, 1);
      --fusion: linear-gradient(120deg, var(--fluor-bright) 0%, var(--fluor) 28%, var(--neon-bright) 58%, var(--purple-deep) 100%);
    }
    .title-fusion {
      background: var(--fusion);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      -webkit-text-fill-color: transparent;
      filter: drop-shadow(0 0 10px rgba(57, 255, 20, 0.45)) drop-shadow(0 0 18px rgba(168, 85, 247, 0.5));
    }
    .neon-data {
      font-family: var(--font-mono);
      font-weight: 500;
      color: var(--fluor-bright);
      text-shadow:
        0 0 6px var(--fluor-glow),
        0 0 18px rgba(57, 255, 20, 0.35),
        0 0 28px rgba(192, 132, 252, 0.25);
    }
    .neon-data--fusion {
      background: var(--fusion);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      -webkit-text-fill-color: transparent;
      filter: drop-shadow(0 0 8px rgba(57, 255, 20, 0.5)) drop-shadow(0 0 14px rgba(168, 85, 247, 0.45));
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }
    ::selection {
      background: rgba(168, 85, 247, 0.4);
      color: #fff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--font-sans);
      color: var(--text);
      background: var(--bg);
      letter-spacing: -0.01em;
      font-size: 15px;
      line-height: 1.45;
      -webkit-font-smoothing: antialiased;
    }
    body::before {
      content: "";
      display: block;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--fluor) 15%, var(--neon-bright) 50%, var(--purple-deep) 85%, transparent);
      box-shadow: 0 0 24px var(--fluor-glow), 0 0 32px var(--glow-strong);
      opacity: 0.95;
      pointer-events: none;
    }
    a:focus-visible {
      outline: 2px solid var(--neon);
      outline-offset: 3px;
      box-shadow: 0 0 0 4px var(--neon-dim);
    }
    .shell {
      position: relative;
      z-index: 1;
      max-width: 1140px;
      margin: 0 auto;
      padding: 36px 28px 56px;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 28px;
      margin-bottom: 36px;
      padding-bottom: 28px;
      border-bottom: 1px solid var(--border-strong);
    }
    .brand {
      display: flex;
      align-items: flex-start;
      gap: 24px;
    }
    .brand-wordmark {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: center;
      min-height: 56px;
      padding: 12px 16px;
      border-radius: var(--radius);
      border: 1px solid var(--border-strong);
      background: var(--surface);
    }
    .brand-wordmark-main {
      font-family: var(--font-sans);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }
    .brand-wordmark-sub {
      margin-top: 6px;
      font-size: 9px;
      font-weight: 600;
      letter-spacing: 0.24em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .hero-tagline {
      margin-top: 8px;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.2em;
      text-transform: uppercase;
    }
    .brand h1 {
      margin: 0;
      font-size: clamp(1.35rem, 2.4vw, 1.6rem);
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.15;
    }
    .brand-sub {
      margin: 10px 0 0;
      font-size: 0.875rem;
      font-weight: 400;
      color: var(--muted);
      max-width: 520px;
      line-height: 1.55;
    }
    .topbar-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: var(--radius);
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.02em;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
    }
    .pulse {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      flex-shrink: 0;
      transition: background 0.25s var(--ease);
    }
    .pulse--warn { background: var(--neon-bright); box-shadow: 0 0 12px var(--glow); }
    .pulse--ok { background: var(--fluor); box-shadow: 0 0 14px var(--fluor-glow); }
    .pulse--err { background: var(--danger); box-shadow: 0 0 10px rgba(251, 113, 133, 0.45); }
    .pill-muted { color: var(--muted); font-weight: 500; }
    .badge {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      padding: 6px 11px;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface);
    }
    .badge-paper {
      border-color: rgba(192, 132, 252, 0.45);
      color: var(--neon-bright);
      background: #0a0a0a;
      text-shadow: 0 0 12px var(--glow);
    }
    .badge-demo {
      border-color: rgba(167, 139, 250, 0.4);
      color: #c4b5fd;
      background: rgba(139, 92, 246, 0.08);
    }
    .badge-live {
      border-color: rgba(251, 113, 133, 0.45);
      color: #fda4af;
      background: rgba(251, 113, 133, 0.08);
      box-shadow: 0 0 14px rgba(251, 113, 133, 0.12);
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(208px, 1fr));
      gap: 1px;
      margin-bottom: 32px;
      background: var(--border);
      border: 1px solid var(--border-strong);
      border-radius: var(--radius);
      overflow: hidden;
    }
    @media (max-width: 520px) {
      .kpis { grid-template-columns: 1fr; }
    }
    .kpi {
      position: relative;
      background: var(--surface);
      padding: 20px 20px 18px;
      border: none;
      border-radius: 0;
      transition: background 0.2s var(--ease);
    }
    .kpi:hover {
      background: var(--surface-muted);
      box-shadow: inset 0 -2px 0 transparent;
      background-image: linear-gradient(90deg, rgba(57, 255, 20, 0.04), rgba(168, 85, 247, 0.06));
    }
    .kpi-head {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 6px;
    }
    .kpi-icon {
      flex-shrink: 0;
      width: 36px;
      height: 36px;
      border-radius: var(--radius);
      display: grid;
      place-items: center;
    }
    .kpi-icon {
      background: #000;
      border: 1px solid var(--border-strong);
      color: var(--fluor);
      box-shadow: 0 0 10px var(--fluor-glow);
    }
    .kpi-icon svg { width: 18px; height: 18px; opacity: 0.85; stroke-width: 1.75; }
    .kpi-label {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--text);
      line-height: 1.2;
    }
    .kpi-value {
      margin-top: 8px;
      font-size: clamp(1.5rem, 2.8vw, 1.85rem);
      font-weight: 600;
      letter-spacing: -0.02em;
      font-variant-numeric: tabular-nums;
      line-height: 1.05;
      font-family: var(--font-mono);
    }
    .kpi-value.neon-data--fusion {
      font-size: clamp(1.5rem, 2.8vw, 1.85rem);
    }
    .kpi-value--pos {
      color: var(--fluor-bright) !important;
      -webkit-text-fill-color: var(--fluor-bright);
      background: none !important;
      filter: none;
      text-shadow: 0 0 8px var(--fluor-glow), 0 0 24px var(--fluor-glow);
    }
    .kpi-value--neg {
      color: var(--danger) !important;
      -webkit-text-fill-color: var(--danger);
      background: none !important;
      filter: none;
      text-shadow: 0 0 18px rgba(255, 77, 109, 0.55);
    }
    .kpi-value--neon {
      color: var(--fluor-bright) !important;
      -webkit-text-fill-color: var(--fluor-bright);
      background: none !important;
      filter: none;
      text-shadow: 0 0 10px var(--fluor-glow), 0 0 28px rgba(192, 132, 252, 0.35);
    }
    .kpi-hint {
      margin-top: 10px;
      font-size: 12px;
      font-weight: 400;
      color: var(--muted);
      line-height: 1.45;
    }

    .context-strip {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px 14px;
      margin-bottom: 20px;
      padding: 11px 16px;
      font-size: 11px;
      font-weight: 500;
      color: var(--muted);
      letter-spacing: 0.03em;
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-left: 3px solid;
      border-image: linear-gradient(180deg, var(--fluor), var(--neon-bright)) 1;
    }
    .context-strip .ctx-label {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      margin-right: 4px;
      background: var(--fusion);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      -webkit-text-fill-color: transparent;
      filter: drop-shadow(0 0 6px rgba(57, 255, 20, 0.35));
    }
    .context-strip .mono {
      font-family: var(--font-mono);
      color: var(--fluor-bright);
      text-shadow: 0 0 8px var(--fluor-glow), 0 0 16px rgba(192, 132, 252, 0.3);
    }
    .context-strip .ctx-sep {
      width: 1px;
      height: 12px;
      background: var(--border-strong);
      flex-shrink: 0;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-radius: var(--radius);
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-elevated);
    }
    .panel-title {
      margin: 0;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .panel-title-dot {
      width: 8px;
      height: 8px;
      border-radius: 1px;
      background: linear-gradient(135deg, var(--fluor), var(--neon-bright));
      box-shadow: 0 0 10px var(--fluor-glow), 0 0 14px var(--glow);
      flex-shrink: 0;
    }
    .panel-meta {
      font-size: 11px;
      font-weight: 500;
      color: var(--muted);
      border-color: rgba(168, 85, 247, 0.2);
      font-variant-numeric: tabular-nums;
      padding: 6px 11px;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface-muted);
      letter-spacing: 0.03em;
    }
    .panel-meta--emph {
      color: var(--fluor-bright);
      font-weight: 600;
      text-shadow: 0 0 10px var(--fluor-glow);
      border-color: rgba(57, 255, 20, 0.25);
      background: #000;
      letter-spacing: 0.08em;
      font-size: 10px;
      text-transform: uppercase;
    }
    .panel-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .table-wrap {
      overflow: auto;
      max-height: min(52vh, 560px);
      -webkit-overflow-scrolling: touch;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    thead th {
      text-align: left;
      padding: 13px 18px;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--text);
      background: #000;
      border-bottom: 1px solid var(--border-strong);
      white-space: nowrap;
      position: sticky;
      top: 0;
      z-index: 2;
      box-shadow: 0 1px 0 var(--border-strong);
    }
    thead th.col-num {
      text-align: right;
    }
    tbody td {
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
      background: var(--surface);
    }
    tbody td.col-num {
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    tbody td.col-state {
      font-size: 12px;
      color: var(--muted);
      max-width: 140px;
    }
    .cell-tp { color: var(--fluor); text-shadow: 0 0 14px var(--fluor-glow); font-weight: 500; }
    .cell-sl { color: var(--danger); text-shadow: 0 0 12px rgba(251, 113, 133, 0.25); }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td {
      background: #111;
      box-shadow: inset 3px 0 0 var(--fluor);
    }
    tbody td.mono.neon-data,
    tbody td.col-num.neon-data {
      font-weight: 500;
    }
    .mono {
      font-family: var(--font-mono);
      font-size: 12px;
      font-weight: 400;
      letter-spacing: -0.01em;
    }
    .side-pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: var(--radius);
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .side-pill.long {
      background: rgba(57, 255, 20, 0.12);
      color: var(--fluor);
      border: 1px solid var(--fluor-dim);
      box-shadow: 0 0 12px rgba(57, 255, 20, 0.2);
    }
    .side-pill.short {
      background: rgba(232, 121, 249, 0.1);
      color: var(--short);
      border: 1px solid rgba(232, 121, 249, 0.35);
    }
    .empty-state {
      padding: 64px 28px 68px;
      text-align: center;
      max-width: 440px;
      margin: 0 auto;
    }
    .empty-state-visual {
      width: 64px;
      height: 64px;
      margin: 0 auto 24px;
      border-radius: var(--radius);
      display: grid;
      place-items: center;
      background: var(--bg-elevated);
      border: 1px solid var(--neon-dim);
      box-shadow: 0 0 28px var(--glow-strong);
    }
    .empty-state-visual svg {
      width: 28px;
      height: 28px;
      opacity: 0.9;
      color: var(--neon-bright);
      filter: drop-shadow(0 0 10px var(--glow));
    }
    .empty-state-title {
      margin: 0;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .empty-state-text {
      margin: 14px 0 0;
      font-size: 13px;
      line-height: 1.55;
      font-weight: 400;
      color: var(--muted);
    }
    .empty-state-hint {
      margin-top: 24px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: center;
      padding: 8px 14px;
      border-radius: var(--radius);
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.04em;
      color: var(--muted);
      border: 1px solid var(--border);
      background: var(--bg-elevated);
    }
    .empty-state-hint code {
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 500;
      color: var(--neon-bright);
      text-shadow: 0 0 10px var(--glow);
    }
    .empty {
      display: none;
    }
    footer {
      margin-top: 36px;
      padding-top: 26px;
      border-top: 1px solid var(--border-strong);
      font-size: 12px;
      color: var(--muted);
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .footer-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      justify-content: space-between;
      align-items: center;
    }
    .footer-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .footer-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 11px;
      border-radius: var(--radius);
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.02em;
      border: 1px solid var(--border);
      background: var(--surface-muted);
      color: var(--text);
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .footer-pill:hover {
      border-color: var(--accent-subtle);
      border-color: var(--neon-dim);
      background: rgba(168, 85, 247, 0.08);
      box-shadow: 0 0 14px var(--glow);
    }
    a.footer-pill {
      color: inherit;
      text-decoration: none;
    }
    .footer-legal {
      font-size: 11px;
      line-height: 1.55;
      max-width: 920px;
      opacity: 0.92;
    }
    footer a {
      color: var(--neon-bright);
      text-decoration: none;
      font-weight: 500;
      text-shadow: 0 0 10px var(--glow);
    }
    footer a:hover { color: var(--neon-bright); text-shadow: 0 0 12px var(--glow); }
    footer a:hover { text-decoration: underline; text-underline-offset: 3px; }
    .err { color: var(--danger); font-size: 13px; }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-wordmark" aria-label="WIA-Software">
          <span class="brand-wordmark-main title-fusion">WIA-Software</span>
          <span class="brand-wordmark-sub">Execution · Risk</span>
        </div>
        <div>
          <p class="hero-tagline title-fusion">Infrastructure · Paper simulation</p>
          <h1 class="title-fusion">Execution Console</h1>
          <p class="brand-sub">TradingView alert orchestration: signal validation, risk controls, and order logging. Paper mode by default; demo and live modes enable the corresponding execution gateway.</p>
        </div>
      </div>
      <div class="topbar-actions">
        <span class="pill"><span class="pulse pulse--warn" id="connPulse" aria-hidden="true"></span><span id="liveLabel" aria-live="polite">Checking status…</span></span>
        <span id="modeBadge" class="badge badge-paper">paper</span>
        <span class="pill pill-muted">Refresh interval · <span id="pollHint">3 s</span></span>
      </div>
    </header>

    <section class="kpis" aria-label="Operational metrics">
      <article class="kpi">
        <div class="kpi-head">
          <span class="kpi-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6M9 15h5"/></svg>
          </span>
          <div class="kpi-label">Operating mode</div>
        </div>
        <div class="kpi-value neon-data--fusion" id="mode">paper</div>
        <div class="kpi-hint">No broker execution until demo or live is enabled.</div>
      </article>
      <article class="kpi">
        <div class="kpi-head">
          <span class="kpi-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 20V10M12 20V4M18 20v-8"/></svg>
          </span>
          <div class="kpi-label">Trades today</div>
        </div>
        <div class="kpi-value neon-data--fusion" id="tradesToday">0</div>
        <div class="kpi-hint">Daily cap from environment variables.</div>
      </article>
      <article class="kpi">
        <div class="kpi-head">
          <span class="kpi-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          </span>
          <div class="kpi-label">Net P&L (today)</div>
        </div>
        <div class="kpi-value neon-data--fusion" id="realizedPnl">+$0.00</div>
        <div class="kpi-hint" id="pnlHint">Record closes via POST /risk/close. Cumulative drawdown · $0.00 · Limit · —</div>
      </article>
      <article class="kpi">
        <div class="kpi-head">
          <span class="kpi-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3v18h18"/><path d="M7 12l4-4 4 4 6-6"/><path d="M21 8V3h-5"/></svg>
          </span>
          <div class="kpi-label">Theoretical TP (open)</div>
        </div>
        <div class="kpi-value neon-data--fusion" id="theoTpOpen">$0.00</div>
        <div class="kpi-hint">Theoretical sum if every open position hits take-profit (fixed-risk model).</div>
      </article>
      <article class="kpi">
        <div class="kpi-head">
          <span class="kpi-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4L4 8l8 4 8-4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/></svg>
          </span>
          <div class="kpi-label">Simulated orders</div>
        </div>
        <div class="kpi-value neon-data--fusion" id="tradeCount">0</div>
        <div class="kpi-hint">Paper positions logged in this session.</div>
      </article>
    </section>

    <div class="context-strip" id="contextStrip" hidden aria-label="Signal validation parameters"></div>

    <section class="panel" aria-labelledby="tbl-title">
      <div class="panel-head">
        <h2 class="panel-title" id="tbl-title"><span class="panel-title-dot" aria-hidden="true"></span><span class="title-fusion">Blotter · paper simulation</span></h2>
        <div class="panel-actions">
          <span class="panel-meta panel-meta--emph" id="blotterCount">0 · rows</span>
          <span class="panel-meta" id="lastSync">—</span>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Instrument</th>
              <th>Side</th>
              <th class="col-num">Price</th>
              <th class="col-num">Size</th>
              <th class="col-num">SL</th>
              <th class="col-num">TP</th>
              <th class="col-num">Risk ($)</th>
              <th class="col-num">At TP ($)</th>
              <th class="col-num">At SL ($)</th>
              <th>TF</th>
              <th>Scores</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody id="trades">
            <tr><td colspan="13">
              <div class="empty-state">
                <div class="empty-state-visual" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M4 18h16"/><path d="M7 14l3-4 4 6 5-9"/><circle cx="17" cy="7" r="2" fill="currentColor" stroke="none"/>
                  </svg>
                </div>
                <h3 class="empty-state-title title-fusion">No activity recorded</h3>
                <p class="empty-state-text">Simulated orders will appear when valid TradingView webhook alerts are received.</p>
                <div class="empty-state-hint"><span>Signal ingress</span> <code>POST /webhook</code></div>
              </div>
            </td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer>
      <div class="footer-row">
        <div class="footer-links">
          <a class="footer-pill" href="/health"><span class="mono">GET</span> /health</a>
          <span class="footer-pill"><span class="mono">POST</span> /webhook</span>
        </div>
        <div class="footer-links">
          <a class="footer-pill" href="/history/paper-trades?limit=100">Paper history</a>
          <a class="footer-pill" href="/history/decisions?limit=200">Decisions</a>
          <a class="footer-pill" href="/history/alerts?limit=200">Alerts</a>
        </div>
        <a class="footer-pill" href="https://www.wia-software.com/" rel="noopener noreferrer" target="_blank">wia-software.com</a>
      </div>
      <p class="footer-legal">
        Technical tool for alert integration and operational simulation. Not investment advice or personalized recommendation.
        Data reflects the current running instance. For institutional deployments and advanced reporting:
        <a href="https://www.wia-software.com/" rel="noopener noreferrer" target="_blank">WIA-Software</a>.
      </p>
      <span id="footerErr" class="err" style="display:none;"></span>
    </footer>
  </div>
  <script>
    const EMPTY_TRADES_ROW = `<tr><td colspan="13"><div class="empty-state"><div class="empty-state-visual" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 18h16"/><path d="M7 14l3-4 4 6 5-9"/><circle cx="17" cy="7" r="2" fill="currentColor" stroke="none"/></svg></div><h3 class="empty-state-title title-fusion">No activity recorded</h3><p class="empty-state-text">Simulated orders will appear when valid TradingView webhook alerts are received.</p><div class="empty-state-hint"><span>Signal ingress</span> <code>POST /webhook</code></div></div></td></tr>`;
    function setPulse(state) {
      const el = document.getElementById('connPulse');
      if (!el) return;
      el.className = 'pulse pulse--' + state;
    }
    function esc(s) {
      return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function modeClass(m) {
      const x = String(m || '').toLowerCase();
      if (x === 'live') return 'badge-live';
      if (x === 'demo') return 'badge-demo';
      return 'badge-paper';
    }
    function fmtUsdPlain(v) {
      if (v == null || v === '') return '—';
      var n = Number(v);
      if (Number.isNaN(n)) return '—';
      return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function fmtSignedUsd(v) {
      if (v == null || v === '') return '—';
      var n = Number(v);
      if (Number.isNaN(n)) return '—';
      return (n >= 0 ? '+$' : '-$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    async function loadHealth() {
      const strip = document.getElementById('contextStrip');
      try {
        const res = await fetch('/health');
        if (!res.ok) throw new Error('Health ' + res.status);
        const h = await res.json();
        const mode = (h.trading_mode || 'paper').toLowerCase();
        document.getElementById('mode').textContent = mode;
        const badge = document.getElementById('modeBadge');
        badge.textContent = mode;
        badge.className = 'badge ' + modeClass(mode);
        document.getElementById('liveLabel').textContent = h.live_trading_enabled ? 'Live execution enabled' : 'Controlled environment (paper / sandbox)';
        setPulse('ok');
        if (strip && h.signal_filters) {
          const sf = h.signal_filters;
          const mapN = typeof h.symbol_map_size === 'number' ? h.symbol_map_size : 0;
          strip.innerHTML =
            '<span class="ctx-label">Active validation</span>' +
            '<span>VWAP / ATR ≤ <span class="mono">' + esc(String(sf.max_dist_vwap_atr)) + '</span></span>' +
            '<span class="ctx-sep" aria-hidden="true"></span>' +
            '<span>Long ≥ <span class="mono">' + esc(String(sf.min_long_score)) + '</span></span>' +
            '<span class="ctx-sep" aria-hidden="true"></span>' +
            '<span>Short ≥ <span class="mono">' + esc(String(sf.min_short_score)) + '</span></span>' +
            '<span class="ctx-sep" aria-hidden="true"></span>' +
            '<span>Symbol map <span class="mono">' + esc(String(mapN)) + '</span></span>';
          strip.hidden = false;
        }
      } catch (e) {
        document.getElementById('liveLabel').textContent = 'Service unavailable';
        setPulse('err');
        if (strip) strip.hidden = true;
      }
    }
    async function refresh() {
      const errEl = document.getElementById('footerErr');
      errEl.style.display = 'none';
      try {
        const res = await fetch('/paper/trades');
        if (!res.ok) throw new Error('API ' + res.status);
        const data = await res.json();
        const trades = data.trades || [];
        const r = data.risk || {};
        const ps = data.pnl_summary || {};
        document.getElementById('tradesToday').textContent = (typeof r.trades_today === 'number') ? r.trades_today : 0;
        var rp = document.getElementById('realizedPnl');
        var realized = typeof r.realized_pnl_today === 'number' ? r.realized_pnl_today : 0;
        if (rp) {
          rp.textContent = fmtSignedUsd(realized);
          rp.classList.remove('kpi-value--pos', 'kpi-value--neg', 'neon-data--fusion');
          if (realized > 0) rp.classList.add('kpi-value--pos');
          else if (realized < 0) rp.classList.add('kpi-value--neg');
          else rp.classList.add('neon-data--fusion');
        }
        var ph = document.getElementById('pnlHint');
        if (ph) {
          var dd = typeof r.daily_loss === 'number' ? fmtUsdPlain(r.daily_loss) : '—';
          var ml = typeof r.max_daily_loss === 'number' ? fmtUsdPlain(r.max_daily_loss) : '—';
          ph.textContent = 'Closes via POST /risk/close. Cumulative drawdown · ' + dd + ' · Limit · ' + ml;
        }
        var tpEl = document.getElementById('theoTpOpen');
        if (tpEl) {
          var ttv = typeof ps.theoretical_tp_usd_open === 'number' ? ps.theoretical_tp_usd_open : 0;
          tpEl.textContent = fmtUsdPlain(ttv);
          tpEl.classList.remove('kpi-value--pos', 'kpi-value--neg', 'kpi-value--neon', 'neon-data--fusion');
          if (ttv > 0) tpEl.classList.add('kpi-value--neon');
          else tpEl.classList.add('neon-data--fusion');
        }
        document.getElementById('tradeCount').textContent = trades.length;
        var blotterEl = document.getElementById('blotterCount');
        if (blotterEl) blotterEl.textContent = trades.length + ' · rows';
        document.getElementById('lastSync').textContent =
          'Last updated · ' + new Date().toLocaleTimeString('en-US');

        const tbody = document.getElementById('trades');
        if (!trades.length) {
          tbody.innerHTML = EMPTY_TRADES_ROW;
          setPulse('ok');
          return;
        }
        tbody.innerHTML = trades.map(function(t) {
          const side = String(t.side || '').toLowerCase();
          const pillClass = side === 'long' ? 'long' : 'short';
          const pillText = side === 'long' ? 'LONG' : 'SHORT';
          return '<tr>' +
            '<td class="mono">' + esc(new Date(t.timestamp).toLocaleString('en-US')) + '</td>' +
            '<td class="mono neon-data">' + esc(t.symbol) + '</td>' +
            '<td><span class="side-pill ' + pillClass + '">' + pillText + '</span></td>' +
            '<td class="mono col-num neon-data">' + esc(t.entry_price) + '</td>' +
            '<td class="mono col-num neon-data">' + esc(t.size) + '</td>' +
            '<td class="mono col-num">' + esc(t.stop_distance) + '</td>' +
            '<td class="mono col-num">' + esc(t.take_profit_distance) + '</td>' +
            '<td class="mono col-num neon-data">' + fmtUsdPlain(t.risk_usd) + '</td>' +
            '<td class="mono col-num cell-tp">' + fmtUsdPlain(t.pnl_if_tp_usd) + '</td>' +
            '<td class="mono col-num cell-sl">' + fmtUsdPlain(t.pnl_if_sl_usd) + '</td>' +
            '<td class="mono">' + esc(t.timeframe) + '</td>' +
            '<td class="mono">L ' + esc(t.long_score) + ' · S ' + esc(t.short_score) + '</td>' +
            '<td class="col-state">' + esc(t.state) + '</td>' +
          '</tr>';
        }).join('');
        setPulse('ok');
      } catch (e) {
        setPulse('err');
        errEl.textContent = 'Failed to load data: ' + e.message;
        errEl.style.display = 'inline';
      }
    }
    loadHealth();
    refresh();
    setInterval(refresh, 3000);
    setInterval(loadHealth, 15000);
  </script>
</body>
</html>
"""
