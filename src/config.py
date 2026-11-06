from datetime import date
from pathlib import Path, PurePosixPath

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Relative paths (.env, sqlite files) are resolved against the project root,
# not the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    # SecretStr masks the values in repr()/str(), logs and tracebacks; use .get_secret_value().
    # Required by main.py. The collector for a separate computer (collect_cli.py) works without them.
    BOT_TOKEN: SecretStr = SecretStr("")
    ADMIN_TG_ID: int = 0
    # The main source of leads (provider 2gis) and manual search.
    # DGIS_API_KEY and the older name TWOGIS_API_KEY both work; DGIS_API_KEY wins.
    TWOGIS_API_KEY: SecretStr | None = None
    DGIS_API_KEY: SecretStr | None = None
    # Demo keys allow 10 items on a page and 5 pages per query. false only for a paid key.
    DGIS_DEMO_KEY: bool = True
    # Ask for items.contact_groups. It needs paid access; without it organizations come without contacts.
    DGIS_REQUEST_CONTACTS: bool = False
    # Requests allowed for one key, 0 = no limit. A demo key has 1000 in total.
    # Collection stops DGIS_REQUEST_RESERVE requests short of the limit.
    DGIS_REQUEST_LIMIT: int = Field(default=1000, ge=0)
    DGIS_REQUEST_RESERVE: int = Field(default=50, ge=0)
    # Optional. A demo key lives 30 days from this date; admins are warned DGIS_KEY_WARN_DAYS before.
    DGIS_KEY_CREATED_AT: date | None = None
    DGIS_KEY_WARN_DAYS: int = Field(default=5, ge=1, le=29)
    DB_URL: str = "sqlite+aiosqlite:///./leads.db"
    # Additional admins, comma-separated. ADMIN_TG_ID stays required and is always an admin.
    ADMIN_TG_IDS: str = ""

    # Group chats for collection results and alerts, comma-separated (groups have negative IDs).
    NOTIFY_CHAT_IDS: str = ""
    # Send the same to admins privately. Without any chats they are told regardless.
    NOTIFY_ADMINS_PRIVATE: bool = True

    # Leads are handed out in batches on request; the pool is refilled when it runs low (0 = never).
    LEADS_BATCH_SIZE: int = Field(default=30, ge=1, le=100)
    LEADS_MIN_POOL: int = Field(default=60, ge=0)
    # false: an organization from 2GIS without a phone or a site is a lead, the owner looks in its card.
    LEADS_REQUIRE_CONTACT: bool = False
    # false: leads with nothing known about the site are not handed out at all.
    LEADS_ISSUE_UNKNOWN: bool = True
    # The largest share of a batch that one niche may take; 1 = no limit.
    LEADS_MAX_NICHE_SHARE: float = Field(default=0.5, gt=0, le=1)
    # Organizations with this many branches, or from this list, are not handed out.
    # The list is comma-separated and replaces the one in src/services/chains.py; empty = that list.
    LEADS_CHAIN_MIN_BRANCHES: int = Field(default=3, ge=2)
    LEADS_CHAIN_BLOCKLIST: str = ""

    # Automatic collection. Search targets and the schedule are validated by src.services.collector.
    # false, or an empty COLLECT_PROVIDERS, means leads come from an external collector as files.
    COLLECT_ENABLED: bool = True
    COLLECT_CRON: str = "0 4 * * *"
    COLLECT_INTERVAL_MINUTES: int | None = Field(default=None, ge=1)  # when set, replaces COLLECT_CRON
    COLLECT_TIMEZONE: str = "Europe/Moscow"
    COLLECT_RUN_ON_START: bool = False
    # 2gis is the main source and needs no access to contacts; without a key it is skipped.
    # 2gis_scraper is opt-in and not listed here: the site answers it with a captcha.
    COLLECT_PROVIDERS: str = "2gis,osm"
    COLLECT_QUERIES: str = ""  # category keys and/or free-text queries; empty = all categories
    COLLECT_DISTRICTS: str = ""  # empty = all SPb districts
    COLLECT_BBOX: str = ""  # 'south,west,north,east'; replaces COLLECT_DISTRICTS
    COLLECT_MAX_PAGES: int = Field(default=2, ge=1, le=100)
    COLLECT_PAGE_SIZE: int = Field(default=50, ge=1, le=50)
    COLLECT_MAX_RESULTS_PER_QUERY: int = Field(default=100, ge=1)
    COLLECT_MAX_REQUESTS_PER_RUN: int = Field(default=60, ge=1)
    COLLECT_MAX_REQUESTS_PER_DAY: int = Field(default=200, ge=1)
    # A fully walked search target of the 2GIS API is not requested again for this long.
    COLLECT_RECHECK_DAYS: int = Field(default=30, ge=1)

    # The city in links to 2GIS cards, for the API provider as well.
    SCRAPER_CITY_SLUG: str = "spb"
    # Reading public pages of 2GIS. The lower bounds keep the bot polite whatever is configured.
    SCRAPER_MIN_DELAY_SECONDS: float = Field(default=3.0, ge=3.0)
    SCRAPER_MAX_DELAY_SECONDS: float = Field(default=8.0, ge=3.0)
    SCRAPER_MAX_PAGES_PER_RUN: int = Field(default=200, ge=1, le=500)
    SCRAPER_MAX_RESULTS_PER_RUN: int = Field(default=300, ge=1, le=500)
    SCRAPER_MIN_HOURS_BETWEEN_RUNS: float = Field(default=20.0, ge=1.0)
    SCRAPER_BLOCK_BACKOFF_HOURS: float = Field(default=6.0, ge=2.0)
    SCRAPER_PAGE_TIMEOUT_SECONDS: float = Field(default=45.0, ge=5.0)

    SCRAPER_HEADLESS: bool = True  # false only for manual debugging on a computer with a screen

    # The collector on a computer with a screen: a person passes a captcha by hand. The bot ignores these.
    SCRAPER_ASSISTED: bool = False
    SCRAPER_PROFILE_DIR: str = "data/browser_profile"
    SCRAPER_ASSISTED_WAIT_MINUTES: float = Field(default=5.0, gt=0, le=30)
    SCRAPER_ASSISTED_MAX_CAPTCHAS: int = Field(default=2, ge=1, le=5)

    # The collector for a separate computer (collect_cli.py). Relative paths are from the project root.
    COLLECTOR_PROVIDERS: str = "2gis"
    COLLECTOR_DB_PATH: str = "data/collector.db"
    COLLECTOR_EXPORT_DIR: str = "exports"

    # Importing export files on the server. Telegram lets bots download at most 20 MB.
    IMPORT_MAX_FILE_BYTES: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    IMPORT_MAX_LEADS: int = Field(default=2000, ge=1)
    IMPORT_INBOX_DIR: str = "data/inbox"
    IMPORT_INBOX_INTERVAL_SECONDS: int = Field(default=300, ge=0)  # 0 = do not watch the folder

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object, info: ValidationInfo) -> object:
        """
        'COLLECT_INTERVAL_MINUTES=' in .env means "not set", not an invalid integer.

        The exception is COLLECT_PROVIDERS: an empty list has a meaning of its own.
        """
        if value == "" and info.field_name and info.field_name != "COLLECT_PROVIDERS":
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @model_validator(mode="after")
    def _use_preferred_key_name(self) -> "Settings":
        if self.DGIS_API_KEY:
            self.TWOGIS_API_KEY = self.DGIS_API_KEY
        return self

    @field_validator("ADMIN_TG_IDS")
    @classmethod
    def _validate_admin_ids(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip() and not part.strip().isdecimal():
                raise ValueError(f"ADMIN_TG_IDS must be comma-separated Telegram IDs, got {part.strip()!r}")
        return value

    @field_validator("NOTIFY_CHAT_IDS")
    @classmethod
    def _validate_notify_chat_ids(cls, value: str) -> str:
        for part in value.split(","):
            chat_id = part.strip()
            if chat_id and (not chat_id.removeprefix("-").isdecimal() or int(chat_id) == 0):
                raise ValueError(
                    f"NOTIFY_CHAT_IDS must be comma-separated chat IDs like -1001234567890, got {chat_id!r}"
                )
        return value

    @property
    def notify_chat_ids(self) -> tuple[int, ...]:
        """Chats for notifications, without repeats."""
        return tuple(dict.fromkeys(int(part) for part in self.NOTIFY_CHAT_IDS.split(",") if part.strip()))

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


def resolve_path(value: str) -> Path:
    """A path from the settings, relative ones counted from the project root."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


settings_error: Exception | None = None

try:
    settings = Settings()
except Exception as exc:  # noqa: BLE001 - any config error is reported by main() before startup
    # Fallback keeps modules importable without .env (e.g. in tests);
    # main.py refuses to start the bot while settings_error is set.
    settings_error = exc
    # model_construct: the invalid value is still in the environment, so validating again would raise here.
    settings = Settings.model_construct(BOT_TOKEN=SecretStr(""), ADMIN_TG_ID=0)
