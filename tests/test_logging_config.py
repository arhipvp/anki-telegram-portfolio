import json
import logging
from pathlib import Path

from anki_telegram_bot.anki import _params_for_log
from anki_telegram_bot.config import Settings
from anki_telegram_bot.logging_config import JsonLogFormatter, configure_logging, redact_secrets, serialize
from anki_telegram_bot.models import CardDraft, UnitType


def test_json_formatter_outputs_valid_json(tmp_path) -> None:
    configure_logging(
        level="DEBUG",
        log_file=tmp_path / "app.log",
        max_bytes=1024,
        backup_count=1,
        full_debug=True,
        secrets=("secret-token",),
    )
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="message",
        args=(),
        exc_info=None,
    )
    record.event = "test.event"
    record.context = {"token": "secret-token", "value": "ok"}

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["event"] == "test.event"
    assert payload["message"] == "message"
    assert payload["context"]["token"] == "***REDACTED***"
    assert payload["context"]["value"] == "ok"


def test_serializer_handles_path_enum_model_and_exception() -> None:
    draft = CardDraft(user_id=1, source_text="страховка", unit_type=UnitType.WORD)
    error = ValueError("bad value")

    serialized = serialize(
        {
            "path": Path("data/app.db"),
            "unit_type": UnitType.WORD,
            "draft": draft,
            "error": error,
        }
    )

    assert serialized["path"] == "data\\app.db" or serialized["path"] == "data/app.db"
    assert serialized["unit_type"] == "word"
    assert serialized["draft"]["source_text"] == "страховка"
    assert serialized["error"] == {"type": "ValueError", "message": "bad value"}


def test_secret_redaction_masks_known_keys_and_secret_values(tmp_path) -> None:
    configure_logging(
        level="DEBUG",
        log_file=tmp_path / "app.log",
        max_bytes=1024,
        backup_count=1,
        full_debug=True,
        secrets=("real-secret",),
    )

    redacted = redact_secrets(
        {
            "Authorization": "Bearer real-secret",
            "POLZA_AI_API_KEY": "real-secret",
            "text": "prefix real-secret suffix",
        }
    )

    assert redacted["Authorization"] == "***REDACTED***"
    assert redacted["POLZA_AI_API_KEY"] == "***REDACTED***"
    assert redacted["text"] == "prefix ***REDACTED*** suffix"


def test_anki_store_media_log_params_do_not_include_base64_payload() -> None:
    params = _params_for_log("storeMediaFile", {"filename": "image.png", "data": "YWJjZA=="})

    assert params["filename"] == "image.png"
    assert params["data"] == "***BASE64_MEDIA_REDACTED:8 chars***"
    assert "YWJjZA==" not in json.dumps(params)


def test_data_url_redaction_masks_image_payload() -> None:
    redacted = redact_secrets({"url": "data:image/jpeg;base64,YWJjZA=="})

    assert redacted["url"] == "***BASE64_IMAGE_REDACTED:8 chars***"
    assert "YWJjZA==" not in json.dumps(redacted)


def test_data_url_redaction_masks_audio_payload() -> None:
    redacted = redact_secrets({"url": "data:audio/ogg;base64,YWJjZA=="})

    assert redacted["url"] == "***BASE64_AUDIO_REDACTED:8 chars***"
    assert "YWJjZA==" not in json.dumps(redacted)


def test_settings_loads_logging_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("POLZA_AI_API_KEY", "polza-key")
    monkeypatch.setenv("APP_LOG_LEVEL", "INFO")
    monkeypatch.setenv("APP_LOG_FILE", str(tmp_path / "custom.log"))
    monkeypatch.setenv("APP_LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("APP_LOG_BACKUP_COUNT", "2")
    monkeypatch.setenv("APP_LOG_FULL_DEBUG", "false")

    settings = Settings()

    assert settings.app_log_level == "INFO"
    assert settings.app_log_file == tmp_path / "custom.log"
    assert settings.app_log_max_bytes == 2048
    assert settings.app_log_backup_count == 2
    assert settings.app_log_full_debug is False


def test_settings_uses_gemini_35_flash_by_default() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="telegram-token",
        POLZA_AI_API_KEY="polza-key",
        _env_file=None,
    )

    assert settings.polza_text_model == "google/gemini-3.5-flash"
