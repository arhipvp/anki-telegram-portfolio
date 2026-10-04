import json
import logging
import re
import sys
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel


_FULL_DEBUG = False
_SECRETS: tuple[str, ...] = ()
_SECRET_KEYS = {
    "authorization",
    "telegram_bot_token",
    "polza_ai_api_key",
    "api_key",
    "token",
}
_DATA_URL_RE = re.compile(r"data:(image|audio)/[^;]+;base64,([A-Za-z0-9+/=]+)")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "context": redact_secrets(serialize(getattr(record, "context", {}))),
        }
        if record.exc_info:
            payload["exception"] = serialize_exception(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    *,
    level: str,
    log_file: Path,
    max_bytes: int,
    backup_count: int,
    full_debug: bool,
    secrets: list[str] | tuple[str, ...] = (),
) -> None:
    global _FULL_DEBUG, _SECRETS
    _FULL_DEBUG = full_debug
    _SECRETS = tuple(secret for secret in secrets if secret)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = JsonLogFormatter()
    resolved_level = getattr(logging, level.upper(), logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(resolved_level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def is_full_debug_enabled() -> bool:
    return _FULL_DEBUG


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str = "",
    exc_info: bool | tuple[type[BaseException], BaseException, TracebackType | None] = False,
    **context: Any,
) -> None:
    logger.log(level, message or event, exc_info=exc_info, extra={"event": event, "context": context})


def serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return serialize(value.model_dump(mode="json"))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize(item) for item in value]
    return value


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SECRET_KEYS:
                redacted[key_text] = "***REDACTED***"
            else:
                redacted[key_text] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        result = value
        for secret in _SECRETS:
            result = result.replace(secret, "***REDACTED***")
        result = _DATA_URL_RE.sub(lambda match: f"***BASE64_{match.group(1).upper()}_REDACTED:{len(match.group(2))} chars***", result)
        return result
    return value


def serialize_exception(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> dict[str, str]:
    exc_type, exc, _ = exc_info
    return {"type": exc_type.__name__, "message": str(exc)}
