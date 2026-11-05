"""Cross-source duplicate detection for places (OSM / 2GIS / CSV have different external IDs)."""

import math
import re
from dataclasses import replace

from src.services.checker import is_social_domain, url_host
from src.services.providers.base import RawPlace
from src.services.twogis_links import twogis_card_url

# Places with the same normalized name closer than this are treated as one business.
SAME_PLACE_MAX_DISTANCE_M: float = 150.0

# Fields copied from a duplicate when the kept place has no value.
_FILLABLE_FIELDS: tuple[str, ...] = (
    "district", "address", "phone", "socials", "map_url", "email", "source_url", "rubric",
)

_PHONE_SEPARATORS_RE = re.compile(r"[,;]")
_NON_DIGITS_RE = re.compile(r"\D")
_NON_WORD_RE = re.compile(r"[^\w]+")
# Coordinates in map_url built by the OSM and 2GIS providers: ...?pt={lon},{lat}...
_MAP_URL_POINT_RE = re.compile(r"[?&]pt=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def normalize_phones(phone: str | None) -> set[str]:
    """
    Normalize a (possibly multi-value) phone string to a set of comparable numbers.

    Russian numbers are reduced to 10 digits, so '+7 (812) 555-01-01', '8 812 5550101'
    and '78125550101' are equal. Extensions ('доб. 123') are dropped. Too short values are ignored.
    """
    numbers: set[str] = set()
    for part in _PHONE_SEPARATORS_RE.split(phone or ""):
        digits = _NON_DIGITS_RE.sub("", part)
        if len(digits) >= 11 and digits[0] in "78":
            digits = digits[1:11]
        if len(digits) >= 7:
            numbers.add(digits)
    return numbers


def normalize_name(name: str | None) -> str:
    """
    Normalize a business name for comparison.

    2GIS appends the business type after a comma ('Улыбка, стоматология'), so only the
    part before the first comma is used. Case, 'ё', quotes and punctuation are ignored.
    """
    primary = (name or "").split(",", 1)[0].lower().replace("ё", "е")
    return " ".join(_NON_WORD_RE.sub(" ", primary).split())


def normalize_address_key(address: str | None) -> str:
    """Normalize an address for comparison: case, 'ё' and punctuation are ignored."""
    lowered = (address or "").lower().replace("ё", "е")
    return " ".join(_NON_WORD_RE.sub(" ", lowered).split())


def coords_from_map_url(map_url: str | None) -> tuple[float, float] | None:
    """Extract (lat, lon) from a provider map URL, or None if it has no coordinates."""
    match = _MAP_URL_POINT_RE.search(map_url or "")
    if not match:
        return None
    lon, lat = float(match.group(1)), float(match.group(2))
    return lat, lon


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Approximate distance in meters between two (lat, lon) points (fine for < 1 km)."""
    mean_lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(mean_lat)
    return math.hypot(dx, dy)


class DuplicateIndex:
    """Lookup of known businesses by normalized phone, name + address, or name + proximity."""

    def __init__(self) -> None:
        self._by_phone: dict[str, int] = {}
        self._by_name: dict[str, list[tuple[int, tuple[float, float]]]] = {}
        self._by_name_address: dict[tuple[str, str], int] = {}

    def add(
        self, key: int, name: str | None, phone: str | None, map_url: str | None, address: str | None = None
    ) -> None:
        """Register a business under the given key."""
        for number in normalize_phones(phone):
            self._by_phone.setdefault(number, key)
        norm_name = normalize_name(name)
        coords = coords_from_map_url(map_url)
        if norm_name and coords:
            self._by_name.setdefault(norm_name, []).append((key, coords))
        norm_address = normalize_address_key(address)
        if norm_name and norm_address:
            self._by_name_address.setdefault((norm_name, norm_address), key)

    def find(
        self, name: str | None, phone: str | None, map_url: str | None, address: str | None = None
    ) -> int | None:
        """Return the key of a matching known business, or None."""
        for number in normalize_phones(phone):
            if number in self._by_phone:
                return self._by_phone[number]
        norm_name = normalize_name(name)
        norm_address = normalize_address_key(address)
        if norm_name and norm_address and (norm_name, norm_address) in self._by_name_address:
            return self._by_name_address[(norm_name, norm_address)]
        coords = coords_from_map_url(map_url)
        if norm_name and coords:
            for key, other in self._by_name.get(norm_name, []):
                if distance_m(coords, other) <= SAME_PLACE_MAX_DISTANCE_M:
                    return key
        return None


def _is_social_url(url: str | None) -> bool:
    return url is not None and is_social_domain(url_host(url))


def _fill_from_duplicate(target: RawPlace, source: RawPlace) -> None:
    """Enrich the kept place with data from its duplicate without overwriting existing values."""
    # A real website beats a social link used as 'website': otherwise a business with
    # a working site would become a false social_only / no_site lead.
    if source.raw_website and (
        not target.raw_website
        or (_is_social_url(target.raw_website) and not _is_social_url(source.raw_website))
    ):
        if target.raw_website and not target.socials:
            target.socials = target.raw_website
        target.raw_website = source.raw_website

    for field in _FILLABLE_FIELDS:
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))

    # The owner looks up contacts on 2GIS, so its card beats a card in another source.
    if twogis_card_url(source.source_url) and not twogis_card_url(target.source_url):
        target.source_url = source.source_url

    target.high_margin = target.high_margin or source.high_margin
    target.contacts_known = target.contacts_known and source.contacts_known


def merge_duplicate_places(places: list[RawPlace]) -> list[RawPlace]:
    """
    Merge places that describe the same business (e.g. found by both OSM and 2GIS).

    The first occurrence keeps its identity (external_id, source, name); missing contacts
    are filled from later duplicates. Input objects are not modified.
    """
    merged: list[RawPlace] = []
    index = DuplicateIndex()
    for place in places:
        key = index.find(place.name, place.phone, place.map_url, place.address)
        if key is None:
            merged.append(replace(place))
            key = len(merged) - 1
        else:
            _fill_from_duplicate(merged[key], place)
        index.add(key, place.name, place.phone, place.map_url, place.address)
    return merged
