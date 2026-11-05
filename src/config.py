from pathlib import Path, PurePosixPath

from pydantic import Field, SecretStr, field_validator
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
    # Additional admins, comma-separated. ADMIN_TG_ID stays required and is always an admin.
    ADMIN_TG_IDS: str = ""

    # Leads are handed out in batches on request; the pool is refilled when it runs low (0 = never).
    LEADS_BATCH_SIZE: int = Field(default=30, ge=1, le=100)
    LEADS_MIN_POOL: int = Field(default=60, ge=0)

    # Automatic collection. Search targets and the schedule are validated by src.services.collector.
    COLLECT_ENABLED: bool = True
    COLLECT_CRON: str = "0 4 * * *"
    COLLECT_INTERVAL_MINUTES: int | None = Field(default=None, ge=1)  # when set, replaces COLLECT_CRON
    COLLECT_TIMEZONE: str = "Europe/Moscow"
    COLLECT_RUN_ON_START: bool = False
    COLLECT_PROVIDERS: str = "2gis,osm"
    COLLECT_QUERIES: str = ""  # category keys and/or free-text queries; empty = all categories
    COLLECT_DISTRICTS: str = ""  # empty = all SPb districts
    COLLECT_BBOX: str = ""  # 'south,west,north,east'; replaces COLLECT_DISTRICTS
    COLLECT_MAX_PAGES: int = Field(default=2, ge=1, le=100)
    COLLECT_PAGE_SIZE: int = Field(default=50, ge=1, le=50)
    COLLECT_MAX_RESULTS_PER_QUERY: int = Field(default=100, ge=1)
    COLLECT_MAX_REQUESTS_PER_RUN: int = Field(default=60, ge=1)
    COLLECT_MAX_REQUESTS_PER_DAY: int = Field(default=200, ge=1)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 'COLLECT_INTERVAL_MINUTES=' in .env means "not set", not an invalid integer.
        env_ignore_empty=True,
    )

    @field_validator("ADMIN_TG_IDS")
    @classmethod
    def _validate_admin_ids(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip() and not part.strip().isdecimal():
                raise ValueError(f"ADMIN_TG_IDS must be comma-separated Telegram IDs, got {part.strip()!r}")
        return value

    @property
    def admin_ids(self) -> frozenset[int]:
        """Telegram IDs allowed to use the bot."""
        return frozenset({self.ADMIN_TG_ID, *(int(part) for part in self.ADMIN_TG_IDS.split(",") if part.strip())})

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
except Exception as exc:  # noqa: BLE001 - any config error is reported by main() before startup
    # Fallback keeps modules importable without .env (e.g. in tests);
    # main.py refuses to start the bot while settings_error is set.
    settings_error = exc
    # model_construct: the invalid value is still in the environment, so validating again would raise here.
    settings = Settings.model_construct(BOT_TOKEN=SecretStr(""), ADMIN_TG_ID=0)
