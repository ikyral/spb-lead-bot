"""Links to organization cards on 2gis.ru: shared by the API provider, the page reader and the bot."""

import re
from urllib.parse import quote, urlsplit

BASE_URL: str = "https://2gis.ru"
DEFAULT_CITY_SLUG: str = "spb"
# Sources whose cards live on 2GIS: an organization without contacts can be looked up there.
TWOGIS_SOURCES: frozenset[str] = frozenset({"2gis", "2gis_scraper"})

_CITY_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*", re.ASCII)
_FIRM_NUMBER_RE = re.compile(r"[0-9]+", re.ASCII)
_HOST_RE = re.compile(r"(?:www\.)?2gis\.[a-z]{2,10}", re.ASCII)
# Telegram refuses a whole message over a bad button, so only an unremarkable link becomes one.
_MAX_BUTTON_URL_LENGTH: int = 512
# Each letter takes 6 characters in a link: this keeps a link to a search within the limit of a button.
_SEARCH_TEXT_LIMIT: int = 75
# Only the host whose robots.txt the page reader follows, and only the page of an organization.
_READABLE_CARD_HOSTS: frozenset[str] = frozenset({"2gis.ru", "www.2gis.ru"})
_CARD_PATH_RE = re.compile(r"(?:/[a-z0-9][a-z0-9_-]*)?/firm/([0-9]+)/?", re.ASCII)


def build_card_url(city_slug: str, firm_id: object) -> str | None:
    """
    Public organization card: https://2gis.ru/<city>/firm/<number>, or None if it cannot be built.

    Places API has no field with this link. Ids may have a hash suffix after '_' that the URL does not use.
    """
    number = str(firm_id or "").split("_", 1)[0]
    if not _FIRM_NUMBER_RE.fullmatch(number) or not _CITY_SLUG_RE.fullmatch(city_slug):
        return None
    return f"{BASE_URL}/{city_slug}/firm/{number}"


def twogis_card_url(url: str | None) -> str | None:
    """The link if it leads to 2GIS and is safe to put on a Telegram button, otherwise None."""
    if not url or len(url) > _MAX_BUTTON_URL_LENGTH or any(char.isspace() for char in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not _HOST_RE.fullmatch(parts.netloc):
        return None
    return url


def readable_card_number(url: str | None) -> str | None:
    """The number of the organization if a browser may open this link as its card, otherwise None."""
    if twogis_card_url(url) is None:
        return None
    parts = urlsplit(str(url))
    if parts.netloc not in _READABLE_CARD_HOSTS or parts.query or parts.fragment:
        return None
    match = _CARD_PATH_RE.fullmatch(parts.path)
    return match.group(1) if match else None


def twogis_search_url(name: str, address: str | None, city_slug: str = DEFAULT_CITY_SLUG) -> str:
    """An ordinary link to a search on 2GIS, for a person to open: the bot itself sends nothing there."""
    city = city_slug if _CITY_SLUG_RE.fullmatch(city_slug) else DEFAULT_CITY_SLUG
    text = " ".join(f"{name} {address or ''}".split())[:_SEARCH_TEXT_LIMIT]
    return f"{BASE_URL}/{city}/search/{quote(text, safe='')}"
