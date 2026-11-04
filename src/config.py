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


try:
    settings = Settings()
except Exception:
    # Fallback default settings when running in environments without .env configured
    settings = Settings(BOT_TOKEN="", ADMIN_TG_ID=0)
