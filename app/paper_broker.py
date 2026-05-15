from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.models import OrderRequest, OrderResult, TradingViewAlert


class PaperBroker:
    def __init__(self) -> None:
        self.trades: list[dict[str, Any]] = []

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
        return OrderResult(
            submitted=True,
            mode="paper",
            reason="simulated paper order",
            deal_reference=deal_reference,
            raw_response=trade,
        )

    def list_trades(self) -> list[dict[str, Any]]:
        return list(reversed(self.trades))
