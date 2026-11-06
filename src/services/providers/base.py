"""Base classes, data models, and constants for map providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

SPB_DISTRICTS: dict[str, tuple[float, float, float, float]] = {
    "Центральный": (59.915, 30.305, 59.955, 30.395),
    "Петроградский": (59.945, 30.250, 59.985, 30.345),
    "Василеостровский": (59.920, 30.200, 59.960, 30.305),
    "Адмиралтейский": (59.895, 30.255, 59.935, 30.340),
    "Московский": (59.810, 30.280, 59.905, 30.365),
    "Приморский": (59.975, 30.140, 60.060, 30.320),
    "Выборгский": (59.970, 30.290, 60.090, 30.400),
    "Калининский": (59.955, 30.350, 60.045, 30.460),
    "Фрунзенский": (59.820, 30.340, 59.915, 30.440),
    "Невский": (59.830, 30.410, 59.935, 30.530),
}

TARGET_CATEGORIES: dict[str, dict[str, Any]] = {
    "dentist_clinic": {
        "title": "🦷 Стоматологии и клиники",
        "osm_tags": [("amenity", "dentist"), ("amenity", "clinic"), ("amenity", "doctors")],
        "query_2gis": "стоматология клиника",
        "high_margin": True,
    },
    "auto_repair": {
        "title": "🚗 Автосервисы и детейлинг",
        "osm_tags": [("shop", "car_repair"), ("craft", "car_painter")],
        "query_2gis": "автосервис кузовной ремонт детейлинг",
        "high_margin": True,
    },
    "beauty": {
        "title": "💇‍♀️ Салоны красоты и косметология",
        "osm_tags": [("shop", "beauty"), ("shop", "hairdresser")],
        "query_2gis": "салон красоты косметология",
        "high_margin": True,
    },
    "legal_b2b": {
        "title": "⚖️ Юристы, бухучет и B2B",
        "osm_tags": [("office", "lawyer"), ("office", "accountant"), ("office", "company")],
        "query_2gis": "юридические услуги бухгалтерские услуги",
        "high_margin": True,
    },
    "horeca": {
        "title": "☕ Кафе, рестораны и пекарни",
        "osm_tags": [("amenity", "restaurant"), ("amenity", "cafe")],
        "query_2gis": "ресторан кафе",
        "high_margin": False,
    },
    "hotels": {
        "title": "🏨 Отели и базы отдыха",
        "osm_tags": [("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "chalet")],
        "query_2gis": "отель гостиница база отдыха",
        "high_margin": False,
    },
    # Small businesses, where a missing site is common and a landing page is an affordable purchase.
    "barbershop": {
        "title": "💈 Барбершопы",
        "osm_tags": [("shop", "hairdresser")],
        "query_2gis": "барбершоп",
        "high_margin": False,
    },
    "nails": {
        "title": "💅 Ногтевые студии",
        "osm_tags": [("shop", "beauty")],
        "query_2gis": "ногтевая студия маникюр",
        "high_margin": False,
    },
    "massage": {
        "title": "💆 Массаж",
        "osm_tags": [("shop", "massage")],
        "query_2gis": "массажный салон",
        "high_margin": True,
    },
    "tire_service": {
        "title": "🛞 Шиномонтаж",
        "osm_tags": [("shop", "tyres")],
        "query_2gis": "шиномонтаж",
        "high_margin": False,
    },
    "detailing": {
        "title": "✨ Детейлинг",
        "osm_tags": [("shop", "car_repair")],
        "query_2gis": "детейлинг",
        "high_margin": True,
    },
    "cleaning": {
        "title": "🧹 Клининг",
        "osm_tags": [("craft", "cleaning")],
        "query_2gis": "клининговые услуги",
        "high_margin": True,
    },
    "renovation": {
        "title": "🏠 Ремонт квартир",
        "osm_tags": [("craft", "builder"), ("craft", "plasterer"), ("craft", "tiler")],
        "query_2gis": "ремонт квартир",
        "high_margin": True,
    },
    "appliance_repair": {
        "title": "🔧 Ремонт техники",
        "osm_tags": [("craft", "electronics_repair"), ("shop", "appliance")],
        "query_2gis": "ремонт бытовой техники",
        "high_margin": False,
    },
    "workshop": {
        "title": "🛠 Мастерские и ателье",
        "osm_tags": [("craft", "tailor"), ("shop", "tailor"), ("craft", "shoemaker"), ("craft", "carpenter")],
        "query_2gis": "мастерская ателье",
        "high_margin": False,
    },
    "tutors": {
        "title": "🎓 Репетиторы и студии",
        "osm_tags": [("amenity", "language_school"), ("amenity", "music_school"), ("amenity", "dancing_school")],
        "query_2gis": "репетитор учебная студия",
        "high_margin": False,
    },
    "photographers": {
        "title": "📷 Фотографы",
        "osm_tags": [("craft", "photographer"), ("shop", "photo")],
        "query_2gis": "фотограф фотостудия",
        "high_margin": False,
    },
    "florists": {
        "title": "💐 Цветочные",
        "osm_tags": [("shop", "florist")],
        "query_2gis": "цветочный магазин",
        "high_margin": False,
    },
}


@dataclass
class RawPlace:
    """Raw place extracted from a data provider before verification."""

    external_id: str
    source: str
    name: str
    category: str
    district: str | None = None
    address: str | None = None
    phone: str | None = None
    socials: str | None = None
    raw_website: str | None = None
    map_url: str | None = None
    high_margin: bool = False
    email: str | None = None  # only an address published in the organization's public card
    source_url: str | None = None  # the organization's card in the source
    rubric: str | None = None  # the source's own category, more precise than the searched niche
    # False: the source gave no contacts, and that does not mean the organization has none.
    contacts_known: bool = True


@dataclass(frozen=True)
class SearchQuery:
    """What to look for: a niche from TARGET_CATEGORIES or a free-text query."""

    text: str
    category: str
    high_margin: bool = False
    osm_tags: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SearchArea:
    """Where to look: (south, west, north, east) bounding box, and the district if it is one."""

    bbox: tuple[float, float, float, float]
    district: str | None = None


@dataclass
class SearchPage:
    """One page of results. `warning` reports a problem with the data that is not a failed request."""

    places: list[RawPlace]
    has_more: bool = False
    warning: str | None = None


class BaseMapProvider(ABC):
    """Abstract base class for external map providers."""

    # Value of COLLECT_PROVIDERS that enables the provider.
    name: str = ""
    # Name of the provider this one replaces: it is used only while that one is out of action.
    fallback_for: str | None = None
    # Sites that must not be visited often: the collector skips the provider until this passes.
    min_hours_between_runs: float = 0.0
    # When set, a rejected key switches the provider off until the key is replaced: this text explains it.
    auth_failure_hint: str | None = None
    # When set, requests are too valuable to repeat: every target continues from the page where it
    # stopped. Progress made under one scope does not count for another (a paid key sees more pages).
    progress_scope: str | None = None
    # A person had to pass a check during this run: the source did not simply let it in,
    # so such a run says nothing about blocks being over and does not clear their count.
    human_check_passed: bool = False
    # The owner chose to run this time before min_hours_between_runs passed. Nothing else is skipped.
    # Such a run says nothing about blocks being over either, so it does not clear their count.
    ignore_interval: bool = False
    # False: the run told nothing about the site (no connection, a format the code does not understand),
    # so it does not use up min_hours_between_runs.
    run_counted: bool = True

    @abstractmethod
    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Interactive search: API errors are logged, whatever was found is returned."""
        ...

    @abstractmethod
    async def search(self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50) -> SearchPage:
        """Collection: one page, one API request. API errors raise ProviderError."""
        ...

    async def close(self) -> None:
        """Called after every run: release whatever the run opened."""
        return None
