import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def configure_logging(logs_dir: Path, log_level: str) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("trading_bot")
    logger.setLevel(log_level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time_gmtime

    app_handler = RotatingFileHandler(
        logs_dir / "app.log",
        maxBytes=5_000_000,
        backupCount=5,
    )
    app_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(app_handler)
    logger.addHandler(stream_handler)
    return logger


def time_gmtime(*_: Any) -> Any:
    import time

    return time.gmtime()


class DecisionLogger:
    def __init__(self, logs_dir: Path) -> None:
        self.alert_log = logs_dir / "alerts.jsonl"
        self.decision_log = logs_dir / "decisions.jsonl"

    def log_alert(self, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.alert_log, {"event": "alert_received", "payload": payload})

    def log_decision(self, decision: dict[str, Any]) -> None:
        self._append_jsonl(self.decision_log, {"event": "decision", **decision})

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, default=str, ensure_ascii=True) + "\n")
