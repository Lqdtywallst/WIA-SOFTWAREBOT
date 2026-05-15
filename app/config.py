from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "TradingView IG Bot"
    trading_mode: Literal["paper", "demo", "live"] = "demo"
    log_level: str = "INFO"
    webhook_secret: Optional[SecretStr] = None

    risk_per_trade: float = Field(default=0.01, gt=0, le=1)
    max_trades_per_day: int = Field(default=3, gt=0)
    max_daily_loss: float = Field(default=500.0, gt=0)
    account_equity: float = Field(default=10_000.0, gt=0)
    default_stop_distance: float = Field(default=20.0, gt=0)
    default_take_profit_distance: float = Field(default=40.0, gt=0)
    default_deal_size: float = Field(default=1.0, gt=0)

    ig_api_key: SecretStr = SecretStr("paper-only")
    ig_identifier: str = "paper-only"
    ig_password: SecretStr = SecretStr("paper-only")
    ig_account_type: Literal["demo", "live"] = "demo"
    ig_account_id: Optional[str] = None

    project_root: Path = Path(__file__).resolve().parent.parent
    logs_dir: Path = project_root / "logs"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("trading_mode", "ig_account_type", mode="before")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        return value.lower().strip()

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("webhook_secret", "ig_account_id", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def live_trading_enabled(self) -> bool:
        return self.trading_mode == "live" and self.ig_account_type == "live"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings
