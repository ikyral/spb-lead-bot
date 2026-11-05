"""Builds the providers enabled in COLLECT_PROVIDERS, explaining why any of them is unavailable."""

from dataclasses import dataclass, field

from src.services.providers.base import BaseMapProvider
from src.services.providers.osm import OSMOverpassProvider
from src.services.providers.twogis import TwoGisProvider
from src.services.providers.yandex import YANDEX_DISABLED_REASON

KNOWN_PROVIDERS: frozenset[str] = frozenset({"2gis", "osm", "yandex"})


@dataclass
class ProviderSet:
    """Usable providers and human-readable reasons for the ones that were turned off."""

    providers: list[BaseMapProvider] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)


def build_providers(enabled: list[str], twogis_api_key: str | None) -> ProviderSet:
    """Create providers from validated names, in the given order (the first source wins on merge)."""
    result = ProviderSet()
    for name in enabled:
        if name == "2gis":
            if twogis_api_key and twogis_api_key.strip():
                result.providers.append(TwoGisProvider(twogis_api_key.strip()))
            else:
                result.disabled[name] = "не задан TWOGIS_API_KEY"
        elif name == "osm":
            result.providers.append(OSMOverpassProvider())
        elif name == "yandex":
            result.disabled[name] = YANDEX_DISABLED_REASON
    return result
