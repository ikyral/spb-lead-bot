"""Builds the providers enabled in COLLECT_PROVIDERS, explaining why any of them is unavailable."""

import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from src.services.providers.base import BaseMapProvider
from src.services.providers.osm import OSMOverpassProvider
from src.services.providers.twogis import TwoGisOptions, TwoGisProvider
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider
from src.services.providers.twogis_scraper import ScraperOptions, TwoGisScraperProvider
from src.services.providers.yandex import YANDEX_DISABLED_REASON

KNOWN_PROVIDERS: frozenset[str] = frozenset({"2gis_scraper", "2gis_cdp", "2gis", "osm", "yandex"})
CDP_MANUAL_ONLY_REASON: str = (
    "запускается только вручную на компьютере с экраном: "
    "python collect_cli.py --assisted (COLLECTOR_PROVIDERS=2gis_cdp)"
)
CDP_NOT_INSTALLED_REASON: str = "не установлен pychrome (pip install -r requirements-cdp.txt)"


@dataclass
class ProviderSet:
    """Usable providers and human-readable reasons for the ones that were turned off."""

    providers: list[BaseMapProvider] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)


def build_providers(
    enabled: list[str],
    twogis_api_key: str | None,
    scraper_options: ScraperOptions | None = None,
    twogis_options: TwoGisOptions | None = None,
    twogis_spend_request: Callable[[], Awaitable[None]] | None = None,
    cdp_options: CdpOptions | None = None,
) -> ProviderSet:
    """
    Create providers from validated names, in the given order (the first source wins on merge).

    2gis_cdp is built only when `cdp_options` is given: only the manual collector does that.
    """
    result = ProviderSet()
    for name in enabled:
        if name == "2gis_cdp":
            if cdp_options is None:
                result.disabled[name] = CDP_MANUAL_ONLY_REASON
            elif importlib.util.find_spec("pychrome") is None:
                result.disabled[name] = CDP_NOT_INSTALLED_REASON
            else:
                result.providers.append(TwoGisCdpProvider(cdp_options))
        elif name == "2gis_scraper":
            if importlib.util.find_spec("playwright") is not None:
                result.providers.append(TwoGisScraperProvider(scraper_options))
            else:
                result.disabled[name] = "не установлен playwright (pip install playwright, playwright install chromium)"
        elif name == "2gis":
            if twogis_api_key and twogis_api_key.strip():
                result.providers.append(
                    TwoGisProvider(twogis_api_key.strip(), options=twogis_options, spend_request=twogis_spend_request)
                )
            else:
                result.disabled[name] = "не задан DGIS_API_KEY"
        elif name == "osm":
            result.providers.append(OSMOverpassProvider())
        elif name == "yandex":
            result.disabled[name] = YANDEX_DISABLED_REASON
    return result
