"""Is a 2GIS card or search page in St. Petersburg? Pure functions, no network."""

import re
from typing import Any

# A conservative box around the whole federal city: Kronstadt, Lomonosov, Petergof, Sestroretsk, Zelenogorsk,
# Kurortny district. Places of the oblast next to the city (Kudrovo, Murino) fall inside it and are kept by the
# POINT rule only (an adm_div city "Кудрово" or "Мурино" is still rejected by the adm_div rule, as intended):
# a missed foreign place is acceptable, a rejected local one is not. The single place for these numbers.
SPB_LAT_MIN: float = 59.55
SPB_LAT_MAX: float = 60.30
SPB_LON_MIN: float = 29.40
SPB_LON_MAX: float = 30.80

_SPB_NAME_PARTS: tuple[str, ...] = ("санкт-петербург", "санкт петербург", "петербург", "спб")
# Towns that belong to the federal city: a card may name them instead of St. Petersburg itself.
_SPB_TOWNS: tuple[str, ...] = (
    "зеленогорск",
    "кронштадт",
    "ломоносов",
    "петергоф",
    "сестрорецк",
    "колпино",
    "пушкин",
    "павловск",
    "красное село",
    "шушары",
    "парголово",
    "стрельна",
    "металлострой",
    "песочный",
    "левашово",
    "репино",
    "комарово",
    "солнечное",
    "молодежное",
    "белоостров",
    "лисий нос",
    "усть-ижора",
    "понтонный",
    "саперный",
    "петродворец",
    "александровская",
    "тярлево",
    "ольгино",
    "лахта",
)
_MAP_CENTER_RE = re.compile(r"[?&]m=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:/|&|$)")


def point_outside_spb(lat: Any, lon: Any) -> bool:
    """True only for a point that is clearly outside the box; a missing or unreadable point is not outside."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return not (SPB_LAT_MIN <= lat_f <= SPB_LAT_MAX and SPB_LON_MIN <= lon_f <= SPB_LON_MAX)


def _normalized(name: str) -> str:
    return name.lower().replace("ё", "е").strip()


def _card_city_names(item: dict[str, Any]) -> list[str]:
    """City names from `adm_div`. UNVERIFIED: no fixture of a real card payload has this field; absent means none."""
    names: list[str] = []
    adm_div = item.get("adm_div")
    for entry in adm_div if isinstance(adm_div, list) else []:
        if isinstance(entry, dict) and entry.get("type") == "city":
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def foreign_card_reason(item: dict[str, Any]) -> str | None:
    """Why a card is not from St. Petersburg, or None when it is, or when nothing says otherwise."""
    point = item.get("point")
    if isinstance(point, dict) and point_outside_spb(point.get("lat"), point.get("lon")):
        return f"point lat={point.get('lat')}, lon={point.get('lon')} is outside St. Petersburg"
    cities = _card_city_names(item)
    if cities:
        normalized = [_normalized(city) for city in cities]
        is_spb = any(part in city for city in normalized for part in _SPB_NAME_PARTS)
        is_town = any(city.startswith(town) for city in normalized for town in _SPB_TOWNS)
        if not is_spb and not is_town:
            return f"city of the card is {', '.join(cities)!r}"
    return None


def foreign_page_reasons(url: str, title: str) -> list[str]:
    """Rules that say a search page may be of another city (a redirect). For a log only."""
    reasons: list[str] = []
    if "санкт-петербург" not in title.lower():
        reasons.append("title has no Санкт-Петербург")
    match = _MAP_CENTER_RE.search(url)
    if match and point_outside_spb(lat=match.group(2), lon=match.group(1)):
        reasons.append(f"map center lon={match.group(1)}, lat={match.group(2)} is outside St. Petersburg")
    return reasons
