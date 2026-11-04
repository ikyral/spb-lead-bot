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
        "high_margin": True,
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


class BaseMapProvider(ABC):
    """Abstract base class for external map providers."""

    @abstractmethod
    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Search places in specified district and category."""
        ...
