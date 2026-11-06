"""
Telling small businesses from chains: a chain will not buy a landing page from a cold message.

Branches are counted by the normalized name over different addresses, in everything collected so
far. A lead of a chain stays in the database, otherwise its branches would not count next time.
"""

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings
from src.database.models import STATUS_NEW, Lead
from src.database.repo import get_lead_fingerprints
from src.services.dedup import normalize_address_key, normalize_name
from src.services.scorer import calculate_lead_score

# Well-known brands, found by whole words in the name. LEADS_CHAIN_BLOCKLIST replaces this list.
DEFAULT_CHAIN_BLOCKLIST: tuple[str, ...] = (
    "пятерочка", "магнит", "дикси", "лента", "перекресток", "ашан", "вкусвилл", "spar", "fix price",
    "макдоналдс", "вкусно и точка", "kfc", "rostics", "ростикс", "бургер кинг", "burger king", "subway", "сабвей",
    "теремок", "додо пицца", "dodo pizza", "папа джонс", "шоколадница", "кофе хауз", "кофе хаус", "cofix",
    "stars coffee",
    "евразия", "токио city", "тануки", "якитория",     "инвитро", "invitro", "гемотест", "хеликс", "ситилаб",
    "kdl", "медси", "см клиника",
    "европейский медицинский центр",
    "сбербанк", "сбер", "втб", "альфа банк", "т банк", "тинькофф", "газпромбанк", "россельхозбанк", "совкомбанк",
    "мтс", "билайн", "мегафон", "теле2", "t2", "ростелеком", "йота",
    "днс", "dns", "м видео", "эльдорадо", "ситилинк", "связной", "леруа мерлен", "лемана про", "петрович", "икеа",
    "сдэк", "cdek", "почта россии", "боксберри", "boxberry", "пэк", "деловые линии",
    "ozon", "озон", "wildberries", "вайлдберриз", "яндекс маркет", "авито",
    "озерки", "горздрав", "ригла", "планета здоровья", "первая помощь", "аптека вита", "36 6",
    "fit service", "фит сервис", "автодок", "emex", "bosch car service", "рольф",
    "топ бьюти", "tony guy", "cut and go", "чио чио", "черная орхидея",
    "world class", "ворлд класс", "x fit", "фитнес хаус", "fitness house", "ддх", "доктор борменталь",
)
# A name made only of these words is what the business does, not a brand: 'Шиномонтаж' at three
# addresses is three different workshops.
_GENERIC_NAME_WORDS: frozenset[str] = frozenset({
    "автомастерская", "автомойка", "автосервис", "автотехцентр", "ателье", "барбершоп", "бар", "букет", "букеты",
    "бухгалтерские", "гостиница", "детейлинг", "для", "дома", "и", "кабинет", "кафе", "квартир", "клиника", "клининг",
    "кондитерская", "косметология", "красоты", "кузовной", "ногтевая", "ногтевой", "маникюр", "массаж", "массажный",
    "мастерская", "мебель", "мини", "нотариус", "обуви", "одежды", "отель", "парикмахерская", "пекарня", "педикюр",
    "пошив", "ремонт", "репетитор", "ресторан", "салон", "сервис", "сервисный", "стоматологическая",
    "стоматологический",
    "стоматология", "студия", "техники", "услуги", "уборка", "фотограф", "фотостудия", "хостел", "цветочный", "цветы",
    "центр", "часов", "химчистка", "шиномонтаж", "юридические", "юрист", "юристы", "магазин",
})
_UPDATE_CHUNK: int = 500


def parse_blocklist(value: str) -> tuple[str, ...]:
    """Brands from the setting, comma-separated; the list from the code if it is empty."""
    brands = tuple(name for name in (normalize_name(part) for part in value.split(",")) if name)
    return brands or tuple(normalize_name(brand) for brand in DEFAULT_CHAIN_BLOCKLIST)


def is_generic_name(normalized: str) -> bool:
    """Is this name only a description of the trade?"""
    return all(word in _GENERIC_NAME_WORDS for word in normalized.split())


class ChainIndex:
    """How many branches every name has among what has been seen."""

    def __init__(self, min_branches: int, blocklist: Iterable[str]) -> None:
        self._min_branches = min_branches
        self._blocklist = tuple(blocklist)
        self._addresses: dict[str, set[str]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChainIndex":
        return cls(settings.LEADS_CHAIN_MIN_BRANCHES, parse_blocklist(settings.LEADS_CHAIN_BLOCKLIST))

    def add(self, name: str | None, address: str | None) -> None:
        """Without an address a place cannot be told from another branch, so it is not counted."""
        normalized, key = normalize_name(name), normalize_address_key(address)
        if normalized and key:
            self._addresses.setdefault(normalized, set()).add(key)

    def branches(self, name: str | None) -> int:
        normalized = normalize_name(name)
        return 0 if is_generic_name(normalized) else len(self._addresses.get(normalized, ()))

    def is_blocked(self, name: str | None) -> bool:
        words = f" {' '.join(normalize_name(name).split())} "
        return any(f" {brand} " in words for brand in self._blocklist)

    def is_chain(self, name: str | None) -> bool:
        return self.is_blocked(name) or self.branches(name) >= self._min_branches


async def load_chain_index(session: AsyncSession, settings: Settings) -> ChainIndex:
    """Everything in the database counts, whatever happened to those leads later."""
    index = ChainIndex.from_settings(settings)
    for name, _phone, _map_url, address in await get_lead_fingerprints(session):
        index.add(name, address)
    return index


async def rescore_new_leads(session: AsyncSession, settings: Settings) -> tuple[int, int]:
    """
    An admin's command: count chains again and score the pool by the current weights.

    Returns how many leads are in the pool now that they are scored and how many scores changed.
    Scores of leads already handed out are history and stay.
    """
    index = await load_chain_index(session, settings)
    await refresh_chain_flags(session, index, reset=True)
    leads = (await session.scalars(select(Lead).where(Lead.crm_status == STATUS_NEW))).all()
    changed = 0
    for lead in leads:
        score = calculate_lead_score(
            lead.category,
            lead.site_status,
            lead.phone,
            lead.socials,
            small_chain=index.branches(lead.name) == 2,
        )
        if score != lead.score:
            lead.score = score
            changed += 1
    await session.commit()
    return len(leads), changed


async def refresh_chain_flags(session: AsyncSession, index: ChainIndex, reset: bool = False) -> int:
    """
    Mark the new leads that turned out to be chains. Returns how many marks were added.

    Marks are not removed automatically: branches merged by a shared phone during a run are not in
    the database, and a later count would forget them. `reset` is for an admin's explicit recount.
    """
    rows = (await session.execute(select(Lead.id, Lead.name, Lead.is_chain).where(Lead.crm_status == STATUS_NEW))).all()
    to_mark = [lead_id for lead_id, name, flag in rows if not flag and index.is_chain(name)]
    to_clear = [lead_id for lead_id, name, flag in rows if flag and not index.is_chain(name)] if reset else []
    for ids, value in ((to_mark, True), (to_clear, False)):
        for start in range(0, len(ids), _UPDATE_CHUNK):
            chunk = ids[start:start + _UPDATE_CHUNK]
            await session.execute(update(Lead).where(Lead.id.in_(chunk)).values(is_chain=value))
    if to_mark or to_clear:
        await session.commit()
    return len(to_mark)
