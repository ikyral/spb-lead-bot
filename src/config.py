import logging
import re
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from src.services.scraper.chrome.proxy import normalize_country, parse_proxy

# Relative paths (.env, sqlite files) are resolved against the project root,
# not the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


class SettingsError(ValueError):
    """The settings cannot work. The message is meant for the owner; any other ValueError is a bug."""


class ProxySettingsError(Exception):
    """
    TWOGIS_PROXY cannot be used. Not a ValueError on purpose: pydantic would print the whole input with
    the error, and the input holds the password of the proxy.
    """


# OUTREACH_OFFER and OUTREACH_SIGNATURE are cut to this many characters.
OUTREACH_TEXT_MAX: int = 300
# SITE_RECHECK_DAYS outside these bounds is brought back into them (with a warning in the log).
SITE_RECHECK_DAYS_MIN: int = 1
SITE_RECHECK_DAYS_MAX: int = 365


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    # SecretStr masks the values in repr()/str(), logs and tracebacks; use .get_secret_value().
    # Required by main.py. The collector for a separate computer (collect_cli.py) works without them.
    BOT_TOKEN: SecretStr = SecretStr("")
    # The main owner of the bot. Leads worked before ownership existed are given to this ID.
    ADMIN_TG_ID: int = 0
    DB_URL: str = "sqlite+aiosqlite:///./leads.db"
    # Additional admins (operators), comma-separated. ADMIN_TG_ID stays required and is always an admin.
    ADMIN_TG_IDS: str = ""
    # Additional owners, comma-separated numeric Telegram IDs: the same rights as ADMIN_TG_ID.
    # An ID in both lists is an owner.
    ADMIN_TG_OWNER_IDS: str = ""

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
    # The first message under a lead without a site: what the owner offers, in one phrase, and how to reach them.
    # Empty = a neutral text from src/bot/texts.py. Longer values are cut, so a card stays within Telegram's limit.
    OUTREACH_OFFER: str = ""
    OUTREACH_SIGNATURE: str = ""
    # The morning plan for admins: reminders, the pool, what waits for enrichment. Time in COLLECT_TIMEZONE.
    DIGEST_ENABLED: bool = True
    DIGEST_TIME: str = "09:30"
    # 'Wrote to them' with no answer for this many days counts as time to remind, without a reminder set.
    FOLLOWUP_AFTER_DAYS: int = Field(default=3, ge=1, le=60)
    # 'Wrote to them' keeps a lead with that person for this many days; then it goes back to the pool.
    LEAD_OWNERSHIP_DAYS: int = Field(default=30, ge=1)
    # Leads of an enrichment request file (/export_unknown) stay with that person, hidden from others, this many hours.
    EXPORT_RESERVE_HOURS: int = Field(default=6, ge=1)

    # Automatic collection. Search targets and the schedule are validated by src.services.collector.
    # false, or an empty COLLECT_PROVIDERS, means leads come from an external collector as files.
    COLLECT_ENABLED: bool = True
    COLLECT_CRON: str = "0 4 * * *"
    COLLECT_INTERVAL_MINUTES: int | None = Field(default=None, ge=1)  # when set, replaces COLLECT_CRON
    COLLECT_TIMEZONE: str = "Europe/Moscow"
    COLLECT_RUN_ON_START: bool = False
    # 2gis_cdp is the main source (a browser, see TWOGIS_*); the removed name 2gis is an alias of it.
    # 2gis_scraper is opt-in and not listed here: the site answers it with a captcha.
    COLLECT_PROVIDERS: str = "2gis_cdp,osm"
    COLLECT_QUERIES: str = ""  # category keys and/or free-text queries; empty = all categories
    COLLECT_DISTRICTS: str = ""  # empty = all SPb districts
    COLLECT_BBOX: str = ""  # 'south,west,north,east'; replaces COLLECT_DISTRICTS
    COLLECT_MAX_PAGES: int = Field(default=2, ge=1, le=100)
    COLLECT_PAGE_SIZE: int = Field(default=50, ge=1, le=50)
    COLLECT_MAX_RESULTS_PER_QUERY: int = Field(default=100, ge=1)
    COLLECT_MAX_REQUESTS_PER_RUN: int = Field(default=60, ge=1)
    COLLECT_MAX_REQUESTS_PER_DAY: int = Field(default=200, ge=1)
    # An organization whose site 2gis_cdp checked (table place_checks) is not clicked again for this many days.
    SITE_RECHECK_DAYS: int = 30

    # The city in links to 2GIS cards, for the API provider as well.
    SCRAPER_CITY_SLUG: str = "spb"
    # Reading public pages of 2GIS. The lower bounds keep the bot polite whatever is configured.
    SCRAPER_MIN_DELAY_SECONDS: float = Field(default=3.0, ge=3.0)
    SCRAPER_MAX_DELAY_SECONDS: float = Field(default=8.0, ge=3.0)
    SCRAPER_MAX_PAGES_PER_RUN: int = Field(default=200, ge=1, le=500)
    SCRAPER_MAX_RESULTS_PER_RUN: int = Field(default=300, ge=1, le=500)
    # 0 = no interval: how often to visit the site is then up to the owner.
    SCRAPER_MIN_HOURS_BETWEEN_RUNS: float = Field(default=20.0, ge=0.0)
    SCRAPER_BLOCK_BACKOFF_HOURS: float = Field(default=6.0, ge=2.0)
    SCRAPER_PAGE_TIMEOUT_SECONDS: float = Field(default=45.0, ge=5.0)

    SCRAPER_HEADLESS: bool = True  # false only for manual debugging on a computer with a screen

    # The collector on a computer with a screen: a person passes a captcha by hand. The bot ignores these.
    SCRAPER_ASSISTED: bool = False
    SCRAPER_PROFILE_DIR: str = "data/browser_profile"
    SCRAPER_ASSISTED_WAIT_MINUTES: float = Field(default=5.0, gt=0, le=30)
    SCRAPER_ASSISTED_MAX_CAPTCHAS: int = Field(default=2, ge=1, le=5)
    # How many times collect_cli.py --check may look at the site within 24 hours.
    SCRAPER_CHECK_MAX_PER_DAY: int = Field(default=3, ge=1, le=10)

    # Парсер 2gis_cdp (Chrome по протоколу DevTools): основной источник лидов, идёт и по расписанию.
    # Нижний предел паузы 3 с задан здесь: меньше поставить нельзя.
    SCRAPER_CDP_MIN_DELAY_SECONDS: float = Field(default=3.0, ge=3.0)
    SCRAPER_CDP_MAX_DELAY_SECONDS: float = Field(default=8.0, ge=3.0)
    # Считаются только загрузки страниц поиска: первая страница цели и переход по пагинации.
    SCRAPER_CDP_MAX_PAGES_PER_RUN: int = Field(default=60, ge=1, le=200)
    SCRAPER_CDP_MAX_RESULTS_PER_RUN: int = Field(default=30, ge=1, le=300)
    # Клики по карточкам за запуск, повторные попытки тоже. 0 = равно SCRAPER_CDP_MAX_RESULTS_PER_RUN;
    # больше него не бывает: значение выше автоматически снижается до SCRAPER_CDP_MAX_RESULTS_PER_RUN.
    SCRAPER_CDP_MAX_CARD_CLICKS_PER_RUN: int = Field(default=0, ge=0, le=300)
    SCRAPER_CDP_MIN_HOURS_BETWEEN_RUNS: float = Field(default=20.0, ge=0.0)
    SCRAPER_CDP_PAGE_TIMEOUT_SECONDS: float = Field(default=45.0, ge=5.0)
    # Пусто = найти Chrome автоматически.
    SCRAPER_CDP_CHROME_PATH: str = ""
    # Прокси с российским IP: схема://[логин:пароль@]хост:порт. SecretStr: пароль не попадает в repr, логи и ошибки.
    TWOGIS_PROXY: SecretStr | None = None
    # Страна внешнего IP, проверяется в самом браузере до первого запроса к 2ГИС. off = не проверять.
    TWOGIS_PROXY_COUNTRY: str = "RU"
    # Профиль Chrome сохраняется между запусками. Старое имя SCRAPER_CDP_PROFILE_DIR тоже читается.
    TWOGIS_PROFILE_DIR: str = Field(
        default="data/cdp_profile", validation_alias=AliasChoices("TWOGIS_PROFILE_DIR", "SCRAPER_CDP_PROFILE_DIR")
    )
    # Часовой пояс и язык браузера: под место прокси.
    TWOGIS_TIMEZONE: str = "Europe/Moscow"
    TWOGIS_LANG: str = "ru-RU"
    # На сервере браузер идёт в обычном режиме под Xvfb; headless только если экрана нет совсем.
    TWOGIS_HEADLESS: bool = False
    # Память Chrome. Лимит кучи V8 в МБ (0 = как в Chrome: 75% оперативной памяти) и облегчённый режим: без картинок,
    # без шрифтов, тайлов карты и стилей, один процесс отрисовки, без расширений.
    TWOGIS_CHROME_MEMORY_MB: int = Field(default=300, ge=0, le=64000)
    TWOGIS_LIGHT_MODE: bool = True
    # Через сколько прочитанных карточек перезапускать Chrome (профиль остаётся), 0 = не перезапускать.
    TWOGIS_RESTART_EVERY_CARDS: int = Field(default=20, ge=0, le=1000)
    # Для диагностики: папка, куда сохраняется каждая страница поиска (HTML и список ссылок). Пусто = не сохранять.
    TWOGIS_SNAPSHOT_DIR: str = ""

    # Looking for the site and contacts of leads on Google, only with collect_cli.py --assisted --google.
    # The upper bounds keep it a person's pace whatever is configured; pauses are fixed in the code.
    GOOGLE_MAX_PER_RUN: int = Field(default=15, ge=1, le=20)
    GOOGLE_MAX_PER_DAY: int = Field(default=60, ge=1, le=100)
    # 0 = no interval, as for the parser of 2GIS: when to run is then up to the owner.
    GOOGLE_MIN_HOURS_BETWEEN_RUNS: float = Field(default=3.0, ge=0.0)
    # How close the name of a lead must be to the address or the title of a result, from 0.5 to 1.
    GOOGLE_NAME_MATCH_THRESHOLD: float = Field(default=0.6, ge=0.5, le=1.0)
    # Cookies of the browser for Google, kept apart from those for 2GIS.
    GOOGLE_PROFILE_DIR: str = "data/google_profile"

    # The collector for a separate computer (collect_cli.py). Relative paths are from the project root.
    COLLECTOR_PROVIDERS: str = "2gis_cdp"
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

    @field_validator("DIGEST_TIME")
    @classmethod
    def _check_digest_time(cls, value: str) -> str:
        hours, _, minutes = value.strip().partition(":")
        if not (hours.isdecimal() and minutes.isdecimal() and int(hours) < 24 and int(minutes) < 60):
            raise ValueError(f"DIGEST_TIME must be HH:MM, for example 09:30, got {value!r}")
        return f"{int(hours):02d}:{int(minutes):02d}"

    def secret_values(self) -> list[str]:
        """Every secret of the settings that is set: for hiding them in texts that are printed or stored."""
        secrets = (self.BOT_TOKEN, self.TWOGIS_PROXY)
        return [value for secret in secrets if secret and (value := secret.get_secret_value().strip())]

    @property
    def digest_hour_minute(self) -> tuple[int, int]:
        hours, minutes = self.DIGEST_TIME.split(":")
        return int(hours), int(minutes)

    @field_validator("OUTREACH_OFFER", "OUTREACH_SIGNATURE")
    @classmethod
    def _trim_outreach(cls, value: str) -> str:
        return " ".join(value.split())[:OUTREACH_TEXT_MAX]

    @field_validator("SITE_RECHECK_DAYS")
    @classmethod
    def _clamp_site_recheck_days(cls, value: int) -> int:
        clamped = min(max(value, SITE_RECHECK_DAYS_MIN), SITE_RECHECK_DAYS_MAX)
        if clamped != value:
            logger.warning(
                "SITE_RECHECK_DAYS=%d is out of %d..%d, using %d",
                value,
                SITE_RECHECK_DAYS_MIN,
                SITE_RECHECK_DAYS_MAX,
                clamped,
            )
        return clamped

    @field_validator("TWOGIS_PROXY_COUNTRY")
    @classmethod
    def _check_proxy_country(cls, value: str) -> str:
        return normalize_country(value)

    @field_validator("TWOGIS_CHROME_MEMORY_MB")
    @classmethod
    def _check_chrome_memory(cls, value: int) -> int:
        if 0 < value < 100:
            raise ValueError("лимит памяти Chrome: от 100 МБ или 0 (по умолчанию Chrome)")
        return value

    @field_validator("TWOGIS_TIMEZONE")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("неизвестный часовой пояс, пример: Europe/Moscow") from None
        return value.strip()

    @field_validator("TWOGIS_LANG")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        text = value.strip()
        if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", text):
            raise ValueError("язык браузера вида ru-RU")
        return text

    @model_validator(mode="after")
    def _check_twogis_proxy(self) -> "Settings":
        # A ProxySettingsError, not a ValueError: the text of a pydantic error repeats the input with the password.
        if self.TWOGIS_PROXY is not None and not self.TWOGIS_PROXY.get_secret_value().strip():
            self.TWOGIS_PROXY = None
        if self.TWOGIS_PROXY is not None:
            try:
                parse_proxy(self.TWOGIS_PROXY.get_secret_value())
            except ValueError as exc:
                raise ProxySettingsError(f"TWOGIS_PROXY: {exc}") from None
        return self

    @model_validator(mode="after")
    def _check_cdp_delays(self) -> "Settings":
        if self.SCRAPER_CDP_MIN_DELAY_SECONDS > self.SCRAPER_CDP_MAX_DELAY_SECONDS:
            raise ValueError("SCRAPER_CDP_MIN_DELAY_SECONDS не может быть больше SCRAPER_CDP_MAX_DELAY_SECONDS")
        return self

    @field_validator("ADMIN_TG_IDS")
    @classmethod
    def _validate_admin_ids(cls, value: str) -> str:
        _check_user_ids("ADMIN_TG_IDS", value)
        return value

    @field_validator("ADMIN_TG_OWNER_IDS")
    @classmethod
    def _validate_owner_ids(cls, value: str) -> str:
        # 0 is not a Telegram user: refused rather than dropped, so a typo does not go unnoticed.
        _check_user_ids("ADMIN_TG_OWNER_IDS", value, allow_zero=False)
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
    def owner_ids(self) -> frozenset[int]:
        """Owners of the bot: ADMIN_TG_ID (when set) and ADMIN_TG_OWNER_IDS."""
        main_owner = {self.ADMIN_TG_ID} if self.ADMIN_TG_ID > 0 else set()
        return frozenset({*main_owner, *_parse_user_ids(self.ADMIN_TG_OWNER_IDS)})

    @property
    def admin_ids(self) -> frozenset[int]:
        """Telegram IDs allowed to use the bot: the owners and the operators."""
        return frozenset({self.ADMIN_TG_ID, *self.owner_ids, *_parse_user_ids(self.ADMIN_TG_IDS)})

    @property
    def has_extra_owners(self) -> bool:
        """ADMIN_TG_OWNER_IDS is set: the bot has more owners than ADMIN_TG_ID."""
        return bool(_parse_user_ids(self.ADMIN_TG_OWNER_IDS))

    @property
    def owner_id(self) -> int:
        """The main owner (ADMIN_TG_ID): leads from before ownership existed go to them. See owner_ids for all."""
        return self.ADMIN_TG_ID

    @property
    def operator_ids(self) -> frozenset[int]:
        """The other admins (ADMIN_TG_IDS): they take and work leads, but do not manage the bot."""
        return self.admin_ids - self.owner_ids - {self.ADMIN_TG_ID}

    def is_owner(self, user_id: int | None) -> bool:
        """May do everything, including what operators may not."""
        return user_id is not None and user_id in self.owner_ids

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
        return value[:start] + resolved + value[start + len(database) :]


def _parse_user_ids(value: str) -> list[int]:
    """'1, ,2,' -> [1, 2]; the value has passed _check_user_ids."""
    return [int(part) for part in value.split(",") if part.strip()]


def _check_user_ids(name: str, value: str, allow_zero: bool = True) -> None:
    for part in value.split(","):
        user_id = part.strip()
        if user_id and (not user_id.isdecimal() or (not allow_zero and int(user_id) == 0)):
            raise ValueError(f"{name} must be comma-separated Telegram IDs, got {user_id!r}")


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
