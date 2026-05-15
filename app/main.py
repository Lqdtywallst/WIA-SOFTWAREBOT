from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.config import get_settings
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
ig_client = IGMarketsClient(settings)
paper_broker = PaperBroker()

app = FastAPI(title=settings.app_name)


@app.on_event("shutdown")
async def shutdown() -> None:
    await ig_client.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "trading_mode": settings.trading_mode,
        "ig_account_type": settings.ig_account_type,
        "live_trading_enabled": settings.live_trading_enabled,
        "dashboard_url": "/dashboard",
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return _dashboard_html()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return _dashboard_html()


@app.get("/paper/trades")
async def paper_trades() -> dict[str, Any]:
    return {"trades": paper_broker.list_trades(), "risk": risk_manager.snapshot()}


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

    signal_decision = evaluate_signal(alert)
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
            order_result = await ig_client.place_market_order(order)
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
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="WIA-Software execution console — TradingView alerts, risk engine, paper simulation.">
  <title>WIA-Software · Execution Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400..700;1,9..40,400..700&family=JetBrains+Mono:wght@450..600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #060912;
      --surface: rgba(17, 24, 39, 0.65);
      --surface-strong: rgba(30, 41, 59, 0.9);
      --border: rgba(148, 163, 184, 0.14);
      --border-strong: rgba(148, 163, 184, 0.22);
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #22d3ee;
      --accent-warm: #fbbf24;
      --accent-dim: rgba(34, 211, 238, 0.14);
      --long: #34d399;
      --short: #fb923c;
      --danger: #f87171;
      --radius: 14px;
      --shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 24px 48px -24px rgba(0,0,0,0.65);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "DM Sans", system-ui, sans-serif;
      font-optical-sizing: auto;
      color: var(--text);
      background:
        radial-gradient(ellipse 120% 80% at 50% -30%, rgba(34, 211, 238, 0.12), transparent 55%),
        radial-gradient(ellipse 70% 50% at 100% 0%, rgba(251, 191, 36, 0.06), transparent 42%),
        var(--bg);
      letter-spacing: -0.01em;
    }
    .shell {
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 28px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      box-shadow: 0 1px 0 rgba(251, 191, 36, 0.12);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-wordmark {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: center;
      min-height: 52px;
      padding: 10px 14px;
      border-radius: 12px;
      background: linear-gradient(155deg, rgba(34, 211, 238, 0.08), rgba(15, 23, 42, 0.85));
      border: 1px solid var(--border-strong);
    }
    .brand-wordmark-main {
      font-family: "JetBrains Mono", ui-monospace, monospace;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--text);
    }
    .brand-wordmark-main .accent { color: var(--accent); }
    .brand-wordmark-sub {
      margin-top: 4px;
      font-size: 9px;
      font-weight: 600;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      color: var(--accent-warm);
    }
    .hero-tagline {
      margin-top: 10px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.28em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .brand h1 {
      margin: 0;
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.03em;
    }
    .brand-sub {
      margin: 6px 0 0;
      font-size: 0.875rem;
      color: var(--muted);
      max-width: 420px;
      line-height: 1.45;
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
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--surface);
      backdrop-filter: blur(12px);
    }
    .pulse {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-dim);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      50% { opacity: 0.55; }
    }
    .pill-muted { color: var(--muted); font-weight: 500; }
    .badge {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 5px 10px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .badge-paper { background: rgba(251, 191, 36, 0.12); color: #fcd34d; border-color: rgba(251, 191, 36, 0.28); }
    .badge-demo { background: rgba(56, 189, 248, 0.12); color: var(--accent); border-color: rgba(56, 189, 248, 0.28); }
    .badge-live { background: rgba(248, 113, 113, 0.14); color: #fca5a5; border-color: rgba(248, 113, 113, 0.35); }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }
    @media (max-width: 900px) {
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 520px) {
      .kpis { grid-template-columns: 1fr; }
    }
    .kpi {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 18px 16px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .kpi-label {
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.11em;
      color: var(--muted);
    }
    .kpi-value {
      margin-top: 10px;
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      font-variant-numeric: tabular-nums;
    }
    .kpi-hint {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.35;
    }

    .panel {
      background: var(--surface-strong);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }
    .panel-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 20px;
      border-bottom: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.35);
    }
    .panel-title {
      margin: 0;
      font-size: 0.95rem;
      font-weight: 650;
    }
    .panel-meta {
      font-size: 12px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .table-wrap {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    thead th {
      text-align: left;
      padding: 12px 16px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      background: rgba(15, 23, 42, 0.65);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    tbody td {
      padding: 14px 16px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.08);
      vertical-align: middle;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: rgba(34, 211, 238, 0.045); }
    .mono {
      font-family: "JetBrains Mono", ui-monospace, monospace;
      font-size: 12px;
      font-weight: 450;
      letter-spacing: -0.02em;
    }
    .side-pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .side-pill.long { background: rgba(52, 211, 153, 0.14); color: var(--long); border: 1px solid rgba(52, 211, 153, 0.35); }
    .side-pill.short { background: rgba(251, 146, 60, 0.14); color: var(--short); border: 1px solid rgba(251, 146, 60, 0.35); }
    .empty {
      padding: 48px 24px;
      text-align: center;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }
    .empty strong { color: var(--text); font-weight: 650; }
    footer {
      margin-top: 24px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--muted);
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .footer-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      justify-content: space-between;
      align-items: center;
    }
    .footer-legal {
      font-size: 11px;
      line-height: 1.55;
      max-width: 920px;
      opacity: 0.92;
    }
    footer a { color: var(--accent); text-decoration: none; font-weight: 600; }
    footer a:hover { text-decoration: underline; }
    .err { color: var(--danger); font-size: 13px; }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-wordmark" aria-label="WIA-Software">
          <span class="brand-wordmark-main"><span class="accent">WIA</span>-Software</span>
          <span class="brand-wordmark-sub">Execution layer</span>
        </div>
        <div>
          <p class="hero-tagline">Next-gen infra · Paper simulation</p>
          <h1>Execution Console</h1>
          <p class="brand-sub">TradingView webhook → validación de señales → gestión de riesgo → conector IG. Vista paper sin ejecución en broker hasta demo/live.</p>
        </div>
      </div>
      <div class="topbar-actions">
        <span class="pill"><span class="pulse" aria-hidden="true"></span><span id="liveLabel">Conectando…</span></span>
        <span id="modeBadge" class="badge badge-paper">paper</span>
        <span class="pill pill-muted">Actualización · <span id="pollHint">3 s</span></span>
      </div>
    </header>

    <section class="kpis" aria-label="Resumen">
      <article class="kpi">
        <div class="kpi-label">Modo operación</div>
        <div class="kpi-value" id="mode">paper</div>
        <div class="kpi-hint">Sin ejecución en broker hasta demo/live.</div>
      </article>
      <article class="kpi">
        <div class="kpi-label">Trades hoy</div>
        <div class="kpi-value" id="tradesToday">0</div>
        <div class="kpi-hint">Límite configurado en Railway / .env.</div>
      </article>
      <article class="kpi">
        <div class="kpi-label">Pérdida diaria</div>
        <div class="kpi-value" id="dailyLoss">0</div>
        <div class="kpi-hint">Solo si registras cierres vía <span class="mono">POST /risk/close</span>.</div>
      </article>
      <article class="kpi">
        <div class="kpi-label">Operaciones simuladas</div>
        <div class="kpi-value" id="tradeCount">0</div>
        <div class="kpi-hint">Últimas posiciones paper en esta instancia.</div>
      </article>
    </section>

    <section class="panel" aria-labelledby="tbl-title">
      <div class="panel-head">
        <h2 class="panel-title" id="tbl-title">Paper blotter · órdenes simuladas</h2>
        <span class="panel-meta" id="lastSync">—</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Hora</th>
              <th>Epic / símbolo</th>
              <th>Lado</th>
              <th>Precio</th>
              <th>Tamaño</th>
              <th>SL</th>
              <th>TP</th>
              <th>TF</th>
              <th>Scores</th>
              <th>Estado</th>
            </tr>
          </thead>
          <tbody id="trades">
            <tr><td colspan="10"><div class="empty"><strong>Sin alertas todavía.</strong><br>Cuando TradingView envíe JSON al webhook, aparecerán aquí.</div></td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer>
      <div class="footer-row">
        <span><span class="mono">GET</span> <a href="/health">/health</a> · <span class="mono">POST</span> <span class="mono">/webhook</span></span>
        <span><a href="https://www.wia-software.com/" rel="noopener noreferrer" target="_blank">wia-software.com</a></span>
      </div>
      <p class="footer-legal">
        Herramienta técnica para recepción de alertas y simulación paper. No constituye recomendación de inversión ni servicio de asesoramiento.
        Los datos mostrados son operativos de la instancia en curso. Para soluciones institucionales y reporting avanzado, consulte
        <a href="https://www.wia-software.com/" rel="noopener noreferrer" target="_blank">WIA-Software</a>.
      </p>
      <span id="footerErr" class="err" style="display:none;"></span>
    </footer>
  </div>
  <script>
    function esc(s) {
      return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function modeClass(m) {
      const x = String(m || '').toLowerCase();
      if (x === 'live') return 'badge-live';
      if (x === 'demo') return 'badge-demo';
      return 'badge-paper';
    }
    async function loadHealth() {
      try {
        const res = await fetch('/health');
        if (!res.ok) throw new Error('Health ' + res.status);
        const h = await res.json();
        const mode = (h.trading_mode || 'paper').toLowerCase();
        document.getElementById('mode').textContent = mode;
        const badge = document.getElementById('modeBadge');
        badge.textContent = mode;
        badge.className = 'badge ' + modeClass(mode);
        document.getElementById('liveLabel').textContent = h.live_trading_enabled ? 'Live permitido' : 'Sandbox / paper';
      } catch (e) {
        document.getElementById('liveLabel').textContent = 'Sin health';
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
        document.getElementById('tradesToday').textContent = (typeof r.trades_today === 'number') ? r.trades_today : 0;
        document.getElementById('dailyLoss').textContent =
          typeof r.daily_loss === 'number' ? r.daily_loss.toFixed(2) : r.daily_loss;
        document.getElementById('tradeCount').textContent = trades.length;
        document.getElementById('lastSync').textContent =
          'Última sync · ' + new Date().toLocaleTimeString();

        const tbody = document.getElementById('trades');
        if (!trades.length) {
          tbody.innerHTML = '<tr><td colspan="10"><div class="empty"><strong>Sin alertas todavía.</strong><br>Cuando TradingView envíe JSON al webhook, aparecerán aquí.</div></td></tr>';
          return;
        }
        tbody.innerHTML = trades.map(function(t) {
          const side = String(t.side || '').toLowerCase();
          const pillClass = side === 'long' ? 'long' : 'short';
          const pillText = side === 'long' ? 'Long' : 'Short';
          return '<tr>' +
            '<td class="mono">' + esc(new Date(t.timestamp).toLocaleString()) + '</td>' +
            '<td class="mono">' + esc(t.symbol) + '</td>' +
            '<td><span class="side-pill ' + pillClass + '">' + pillText + '</span></td>' +
            '<td class="mono">' + esc(t.entry_price) + '</td>' +
            '<td class="mono">' + esc(t.size) + '</td>' +
            '<td class="mono">' + esc(t.stop_distance) + '</td>' +
            '<td class="mono">' + esc(t.take_profit_distance) + '</td>' +
            '<td class="mono">' + esc(t.timeframe) + '</td>' +
            '<td class="mono">L ' + esc(t.long_score) + ' · S ' + esc(t.short_score) + '</td>' +
            '<td>' + esc(t.state) + '</td>' +
          '</tr>';
        }).join('');
      } catch (e) {
        errEl.textContent = 'Error al cargar datos: ' + e.message;
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
