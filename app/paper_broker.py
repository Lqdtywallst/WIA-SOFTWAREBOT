import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from app.models import OrderRequest, OrderResult, TradingViewAlert


class PaperBroker:
    def __init__(self, logs_dir: Optional[Path] = None) -> None:
        self.trades: list[dict[str, Any]] = []
        self._history_file = logs_dir / "paper_trades.jsonl" if logs_dir is not None else None

    def _append_history(self, trade: dict[str, Any]) -> None:
        if self._history_file is None:
            return
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        record = dict(trade)
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        with self._history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, default=str, ensure_ascii=True) + "\n")

    async def place_market_order(
        self,
        order: OrderRequest,
        alert: TradingViewAlert,
    ) -> OrderResult:
        deal_reference = f"PAPER-{uuid4().hex[:12].upper()}"
        trade = {
            "deal_reference": deal_reference,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": order.symbol,
            "side": order.side.value,
            "entry_price": alert.price,
            "size": order.size,
            "stop_distance": order.stop_distance,
            "take_profit_distance": order.take_profit_distance,
            "timeframe": alert.timeframe,
            "bias": alert.bias,
            "state": alert.state,
            "long_score": alert.long_score,
            "short_score": alert.short_score,
            "dist_vwap_atr": alert.dist_vwap_atr,
            "status": "open",
        }
        self.trades.append(trade)
        self._append_history(trade)
        return OrderResult(
            submitted=True,
            mode="paper",
            reason="simulated paper order",
            deal_reference=deal_reference,
            raw_response=trade,
        )

    def list_trades(self) -> list[dict[str, Any]]:
        return list(reversed(self.trades))
