from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class TradeSide(str, Enum):
    long = "long"
    short = "short"


class TradingViewAlert(BaseModel):
    symbol: str = Field(min_length=1)
    side: TradeSide
    price: float = Field(gt=0)
    bias: str = Field(min_length=1)
    state: str = Field(min_length=1)
    long_score: float = Field(ge=0)
    short_score: float = Field(ge=0)
    dist_vwap_atr: float = Field(ge=0)
    timeframe: str = Field(min_length=1)
    secret: Optional[str] = None

    @field_validator("symbol", "bias", "state", "timeframe", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.strip()

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.lower().strip()


class TradeDecision(BaseModel):
    allowed: bool
    reason: str
    alert: TradingViewAlert


class OrderRequest(BaseModel):
    symbol: str
    side: TradeSide
    size: float
    stop_distance: float
    take_profit_distance: float


class OrderResult(BaseModel):
    submitted: bool
    mode: str
    reason: Optional[str] = None
    deal_reference: Optional[str] = None
    raw_response: Optional[dict[str, Any]] = None


class PnLUpdate(BaseModel):
    symbol: str = Field(min_length=1)
    realized_pnl: float
