"""What a 2GIS API key allows: the requests spent with it over its whole life and its expiry date."""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.database.repo import spend_provider_request, sync_provider_key
from src.services.api_client import ProviderQuotaError

DGIS_PROVIDER_NAME: str = "2gis"
# A free demo key is valid for a month from the day it was created.
KEY_LIFETIME_DAYS: int = 30


def key_fingerprint(api_key: str) -> str:
    """Tells keys apart in the database without storing them."""
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class DgisKeyPolicy:
    """Limits of the key, from the settings."""

    limit: int = 1000  # 0 = no limit
    reserve: int = 50
    created_at: date | None = None
    warn_days: int = 5
    request_contacts: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "DgisKeyPolicy":
        return cls(
            limit=settings.DGIS_REQUEST_LIMIT,
            reserve=settings.DGIS_REQUEST_RESERVE,
            created_at=settings.DGIS_KEY_CREATED_AT,
            warn_days=settings.DGIS_KEY_WARN_DAYS,
            request_contacts=settings.DGIS_REQUEST_CONTACTS,
        )

    @property
    def ceiling(self) -> int | None:
        """How many requests may be spent, None if there is no limit."""
        return max(0, self.limit - self.reserve) if self.limit else None

    @property
    def expires_on(self) -> date | None:
        return self.created_at + timedelta(days=KEY_LIFETIME_DAYS) if self.created_at else None


def key_expiry_notice(policy: DgisKeyPolicy, today: date) -> tuple[str, str] | None:
    """
    A warning about the key running out, if it is time for one: (mark, text).

    The mark tells warnings apart: one a day while the time runs out, one when it is over.
    """
    expires_on = policy.expires_on
    if expires_on is None:
        return None
    days_left = (expires_on - today).days
    if days_left > policy.warn_days:
        return None
    shown = expires_on.strftime("%d.%m.%Y")
    consequences = (
        "Накопленные лиды продолжат выдаваться. Новый ключ укажите в DGIS_API_KEY, дату в DGIS_KEY_CREATED_AT."
    )
    if days_left < 0:
        return (
            f"{expires_on}:expired",
            f"⌛ Срок ключа 2ГИС закончился {shown} (по дате в DGIS_KEY_CREATED_AT). Как только API откажет, "
            f"сбор через него отключится. {consequences}",
        )
    return (
        f"{expires_on}:{today}",
        f"⏳ Ключ 2ГИС действует до {shown}, осталось дней: {days_left}. {consequences}",
    )


class DgisQuota:
    """
    Counts every request sent with the key, retries and manual searches included.

    The daily and per-run limits of the collector do not cover this: they restart and do not
    see retries, while going over the total gets a demo key blocked. A request is counted before
    it is sent, so a failed one is counted too: the counter may overstate, never understate.
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession], api_key: str, policy: DgisKeyPolicy) -> None:
        self._session_maker = session_maker
        self._fingerprint = key_fingerprint(api_key)
        self.policy = policy

    async def sync_key(self) -> None:
        """Call when the bot starts: a replaced key gets a clean start."""
        async with self._session_maker() as session:
            await sync_provider_key(session, DGIS_PROVIDER_NAME, self._fingerprint)

    async def spend(self) -> None:
        """Count one request, or refuse it. The message is meant for the admin."""
        async with self._session_maker() as session:
            allowed = await spend_provider_request(session, DGIS_PROVIDER_NAME, self._fingerprint, self.policy.ceiling)
        if not allowed:
            raise ProviderQuotaError(
                f"2ГИС: израсходован лимит ключа: {self.policy.ceiling} запросов из {self.policy.limit} "
                f"(резерв {self.policy.reserve}). Сбор через API остановлен, накопленные лиды выдаются как обычно."
            )
