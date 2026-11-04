from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    BOT_TOKEN: str
    ADMIN_TG_ID: int
    TWOGIS_API_KEY: str | None = None
    DB_URL: str = "sqlite+aiosqlite:///./leads.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings_error: Exception | None = None

try:
    settings = Settings()
except Exception as exc:
    # Fallback keeps modules importable without .env (e.g. in tests);
    # main.py refuses to start the bot while settings_error is set.
    settings_error = exc
    settings = Settings(BOT_TOKEN="", ADMIN_TG_ID=0)
