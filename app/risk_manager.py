from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Tuple

from app.config import Settings
from app.models import OrderRequest, TradeDecision


@dataclass
class RiskState:
    trading_day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    open_symbols: set[str] = field(default_factory=set)


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = RiskState()

    def build_order(self, decision: TradeDecision) -> Tuple[bool, str, Optional[OrderRequest]]:
        self._reset_if_new_day()
        alert = decision.alert
        symbol = alert.symbol.upper()

        if not decision.allowed:
            return False, decision.reason, None

        if symbol in self.state.open_symbols:
            return False, f"trade already open for {symbol}", None

        if self.state.trades_today >= self.settings.max_trades_per_day:
            return False, "max trades per day reached", None

        if self.daily_loss >= self.settings.max_daily_loss:
            return False, "max daily loss reached", None

        size = self._calculate_position_size(self.settings.default_stop_distance)
        return True, "risk checks passed", OrderRequest(
            symbol=symbol,
            side=alert.side,
            size=size,
            stop_distance=self.settings.default_stop_distance,
            take_profit_distance=self.settings.default_take_profit_distance,
        )

    def register_open_trade(self, symbol: str) -> None:
        self._reset_if_new_day()
        self.state.open_symbols.add(symbol.upper())
        self.state.trades_today += 1

    def close_trade(self, symbol: str, realized_pnl: float) -> None:
        self._reset_if_new_day()
        self.state.open_symbols.discard(symbol.upper())
        self.state.realized_pnl_today += realized_pnl

    @property
    def daily_loss(self) -> float:
        return max(0.0, -self.state.realized_pnl_today)

    def snapshot(self) -> dict[str, object]:
        self._reset_if_new_day()
        return {
            "trading_day": self.state.trading_day.isoformat(),
            "trades_today": self.state.trades_today,
            "max_trades_per_day": self.settings.max_trades_per_day,
            "realized_pnl_today": self.state.realized_pnl_today,
            "daily_loss": self.daily_loss,
            "max_daily_loss": self.settings.max_daily_loss,
            "open_symbols": sorted(self.state.open_symbols),
            "risk_per_trade": self.settings.risk_per_trade,
        }

    def _calculate_position_size(self, stop_distance: float) -> float:
        risk_amount = self.settings.account_equity * self.settings.risk_per_trade
        risk_based_size = risk_amount / stop_distance
        return round(min(self.settings.default_deal_size, risk_based_size), 2)

    def _reset_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self.state.trading_day != today:
            self.state = RiskState(trading_day=today)
