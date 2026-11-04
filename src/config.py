from pathlib import Path, PurePosixPath

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Relative paths (.env, sqlite files) are resolved against the project root,
# not the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    # SecretStr masks the values in repr()/str(), logs and tracebacks; use .get_secret_value().
    BOT_TOKEN: SecretStr
    ADMIN_TG_ID: int
    TWOGIS_API_KEY: SecretStr | None = None
    DB_URL: str = "sqlite+aiosqlite:///./leads.db"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("DB_URL")
    @classmethod
    def _resolve_relative_sqlite_path(cls, value: str) -> str:
        """'sqlite:///./leads.db' must not depend on the directory the bot was started from."""
        url = make_url(value)
        database = url.database
        if (
            not url.drivername.startswith("sqlite")
            or not database
            or database == ":memory:"
            or database.startswith("file:")
            or PurePosixPath(database).is_absolute()
            or Path(database).is_absolute()
        ):
            return value
        resolved = (PROJECT_ROOT / database).resolve().as_posix()
        # Replace the path in the original string: keeps driver and query params, and avoids
        # URL-escaping a Windows drive ('C:' -> 'C%3A') that render_as_string would do.
        start = value.index(database)
        return value[:start] + resolved + value[start + len(database):]


settings_error: Exception | None = None

try:
    settings = Settings()
except Exception as exc:
    # Fallback keeps modules importable without .env (e.g. in tests);
    # main.py refuses to start the bot while settings_error is set.
    settings_error = exc
    settings = Settings(BOT_TOKEN="", ADMIN_TG_ID=0)
