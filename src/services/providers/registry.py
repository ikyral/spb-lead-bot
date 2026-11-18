"""Builds the providers enabled in COLLECT_PROVIDERS, explaining why any of them is unavailable."""

import importlib.util
import logging
from dataclasses import dataclass, field

from src.services.providers.base import BaseMapProvider
from src.services.providers.osm import OSMOverpassProvider
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider
from src.services.providers.twogis_scraper import ScraperOptions, TwoGisScraperProvider
from src.services.providers.yandex import YANDEX_DISABLED_REASON

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: frozenset[str] = frozenset({"2gis_scraper", "2gis_cdp", "osm", "yandex"})
# The API of 2GIS is gone: the old name of the provider now means the browser one.
LEGACY_PROVIDER_ALIASES: dict[str, str] = {"2gis": "2gis_cdp"}
CDP_MANUAL_ONLY_REASON: str = (
    "запускается только вручную на компьютере с экраном: "
    "python collect_cli.py --assisted (COLLECTOR_PROVIDERS=2gis_cdp)"
)
CDP_NOT_INSTALLED_REASON: str = "не установлен pychrome (pip install -r requirements-cdp.txt)"


def resolve_provider_aliases(names: list[str], setting: str) -> list[str]:
    """Replace the removed provider names by their successors, warn about it, keep the order, drop repeats."""
    result: list[str] = []
    for name in names:
        target = LEGACY_PROVIDER_ALIASES.get(name, name)
        if target != name:
            logger.warning("%s: провайдер %s удалён, используется %s. Замените имя в .env", setting, name, target)
        if target not in result:
            result.append(target)
    return result


@dataclass
class ProviderSet:
    """Usable providers and human-readable reasons for the ones that were turned off."""

    providers: list[BaseMapProvider] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)


def build_providers(
    enabled: list[str],
    scraper_options: ScraperOptions | None = None,
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
        elif name == "osm":
            result.providers.append(OSMOverpassProvider())
        elif name == "yandex":
            result.disabled[name] = YANDEX_DISABLED_REASON
    return result
