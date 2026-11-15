"""Links to organization cards on 2gis.ru: shared by the API provider, the page reader and the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from src.services.scraper.snapshot import PageSnapshot

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

_CATALOG_HOST_RE = re.compile(r"^catalog\.api\.2gis\.(?:ru|com)$")
_CARD_PATH = "/3.0/items/byid"
_ANCHOR_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_SITE_OWN_HOST_RE = re.compile(r"(?:^|\.)2gis\.(?:ru|com)$")
FROM_LINKS_KEY: str = "__from_links__"

_CAPTCHA_HOST = "captcha.2gis."
_CAPTCHA_TITLE = "captcha"
_CAPTCHA_HTML_MARKERS: tuple[str, ...] = ("g-recaptcha", "подозрительную активность", "что вы не робот")
_REFUSING_STATUSES: frozenset[int] = frozenset({401, 403, 429})


def build_card_url(city_slug: str, firm_id: object) -> str | None:
    """
    Public organization card: https://2gis.ru/<city>/firm/<number>, or None if it cannot be built.

    Places API has no field with this link. Ids may have a hash suffix after '_' that the URL does not use.
    """
    number = str(firm_id or "").split("_", 1)[0]
    if not _FIRM_NUMBER_RE.fullmatch(number) or not _CITY_SLUG_RE.fullmatch(city_slug):
        return None
    return f"{BASE_URL}/{city_slug}/firm/{number}"


def build_search_url(city_slug: str, text: str, page: int = 1) -> str:
    """Public search page: https://2gis.ru/spb/search/<text>[/page/<n>]."""
    url = f"{BASE_URL}/{city_slug}/search/{quote(text, safe='')}"
    return f"{url}/page/{page}" if page > 1 else url


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


def is_captcha(final_url: str, title: str, html: str) -> bool:
    """Is this page the captcha of the site?"""
    if urlsplit(final_url).netloc.startswith(_CAPTCHA_HOST) or _CAPTCHA_TITLE in title.lower():
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _CAPTCHA_HTML_MARKERS)


def detect_block(status: int | None, final_url: str, title: str, html: str) -> str | None:
    """Return why the site refused to show the page, or None if it did not."""
    if status in _REFUSING_STATUSES:
        return f"сайт ответил кодом {status}"
    if is_captcha(final_url, title, html):
        return "сайт показал капчу"
    return None


def refusal_in_bodies(bodies: list[dict[str, Any]]) -> str | None:
    """The page may load, while the requests of its scripts are refused."""
    for body in bodies:
        meta = body.get("meta")
        code = meta.get("code") if isinstance(meta, dict) else None
        if isinstance(code, int) and code in _REFUSING_STATUSES:
            return f"сайт отказал в данных (код {code})"
    return None


_ITEMS_PATH = "/3.0/items"


@dataclass(frozen=True)
class SearchData:
    items: list[dict[str, Any]]
    total: int | None = None


def is_search_response(url: str) -> bool:
    """Is this response of the page's own scripts a list of organizations?"""
    parts = urlsplit(url)
    return bool(_CATALOG_HOST_RE.match(parts.netloc)) and parts.path.rstrip("/") == _ITEMS_PATH


def read_search_bodies(bodies: list[dict[str, Any]]) -> SearchData | None:
    """Find the search result among the responses of a page."""
    for body in bodies:
        result = body.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        if isinstance(items, list):
            total = result.get("total") if isinstance(result, dict) else None
            return SearchData(
                items=[item for item in items if isinstance(item, dict)],
                total=total if isinstance(total, int) else None,
            )
    for body in bodies:
        meta = body.get("meta")
        code = meta.get("code") if isinstance(meta, dict) else None
        if code == 404:
            return SearchData(items=[], total=0)
    return None


def is_card_response(url: str) -> bool:
    """Is this response of the page's own scripts an organization card?"""
    parts = urlsplit(url)
    return bool(_CATALOG_HOST_RE.match(parts.netloc)) and parts.path.rstrip("/") == _CARD_PATH


def read_card_body(bodies: list[dict[str, Any]], firm_id: str) -> dict[str, Any] | None:
    """Find the card of this organization among the responses of a page."""
    numeric_id = firm_id.split("_", 1)[0]
    for body in bodies:
        result = body.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("id", "")).split("_", 1)[0] == numeric_id:
                    return item
    return None


def read_card_links(snapshot: PageSnapshot, firm_id: str) -> dict[str, Any] | None:
    """Extract card contacts from page HTML anchors as a fallback."""
    from src.services.checker import is_social_domain, url_host

    if f"/firm/{firm_id.split('_', 1)[0]}" not in urlsplit(snapshot.final_url).path:
        return None
    contacts: list[dict[str, str]] = []
    sites: dict[str, str] = {}
    for href in dict.fromkeys(_ANCHOR_HREF_RE.findall(snapshot.html)):
        if href.startswith("tel:"):
            contacts.append({"type": "phone", "value": href.removeprefix("tel:")})
        elif href.startswith("mailto:"):
            contacts.append({"type": "email", "value": href.removeprefix("mailto:").split("?", 1)[0]})
        elif href.startswith(("http://", "https://")):
            host = (url_host(href) or "").lower()
            if is_social_domain(host):
                contacts.append({"type": "social", "value": href})
            elif host and not _SITE_OWN_HOST_RE.search(host):
                sites.setdefault(host, href)
    if len(sites) == 1:
        contacts.append({"type": "website", "value": next(iter(sites.values()))})
    if not contacts:
        return None
    return {"id": firm_id, "contact_groups": [{"contacts": contacts}], FROM_LINKS_KEY: True}
