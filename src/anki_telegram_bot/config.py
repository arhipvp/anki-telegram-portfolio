from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .anki_format import NOTE_TYPE_NAME


class Settings(BaseSettings):
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    polza_ai_api_key: str = Field(alias="POLZA_AI_API_KEY")
    allowed_telegram_user_ids_raw: str = Field("", alias="ALLOWED_TELEGRAM_USER_IDS")
    telegram_user_profiles_raw: str = Field("", alias="TELEGRAM_USER_PROFILES")
    anki_connect_url: str = Field("http://127.0.0.1:8765", alias="ANKI_CONNECT_URL")
    anki_deck_name: str = Field("German", alias="ANKI_DECK_NAME")
    anki_note_type: str = Field(NOTE_TYPE_NAME, alias="ANKI_NOTE_TYPE")
    polza_base_url: str = Field("https://polza.ai/api/v1", alias="POLZA_BASE_URL")
    polza_text_model: str = Field("google/gemini-3.5-flash", alias="POLZA_TEXT_MODEL")
    polza_image_model: str = Field("google/gemini-2.5-flash-image", alias="POLZA_IMAGE_MODEL")
    polza_transcription_model: str = Field("openai/gpt-4o-mini-transcribe", alias="POLZA_TRANSCRIPTION_MODEL")
    app_db_path: Path = Field(Path("data/app.db"), alias="APP_DB_PATH")
    app_media_dir: Path = Field(Path("data/media"), alias="APP_MEDIA_DIR")
    ffmpeg_bin: str = Field("ffmpeg", alias="FFMPEG_BIN")
    voice_max_bytes: int = Field(25_000_000, alias="VOICE_MAX_BYTES")
    app_log_level: str = Field("DEBUG", alias="APP_LOG_LEVEL")
    app_log_file: Path = Field(Path("logs/app.log"), alias="APP_LOG_FILE")
    app_log_max_bytes: int = Field(10_485_760, alias="APP_LOG_MAX_BYTES")
    app_log_backup_count: int = Field(5, alias="APP_LOG_BACKUP_COUNT")
    app_log_full_debug: bool = Field(True, alias="APP_LOG_FULL_DEBUG")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    @property
    def allowed_telegram_user_ids(self) -> set[int]:
        values = [value.strip() for value in self.allowed_telegram_user_ids_raw.split(",")]
        return {int(value) for value in values if value}

    @property
    def user_profiles(self):
        from .profiles import parse_user_profiles

        return parse_user_profiles(
            self.telegram_user_profiles_raw,
            self.allowed_telegram_user_ids,
            self.anki_deck_name,
        )


def load_settings() -> Settings:
    load_dotenv(encoding="utf-8")
    settings = Settings()
    settings.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.app_media_dir.mkdir(parents=True, exist_ok=True)
    settings.app_log_file.parent.mkdir(parents=True, exist_ok=True)
    return settings
