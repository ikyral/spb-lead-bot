"""
Everything that depends on how 2gis.ru looks: URLs, signs of a block, the format of responses.

When the site changes, this is the file to edit.

STATUS OF THIS FILE (2026-11-05). The block detection is based on a page really received from
the site (tests/fixtures/twogis_captcha.html). The rest was never checked against the site:
the very first request was answered with a captcha, so no search page or card was seen.
- The search and card URLs are the public ones known from the site's links.
- Which requests the site's own scripts send, and that their format equals the documented
  Catalog API 3.0, are assumptions. If they are wrong, pages are reported as unrecognized
  (the provider stops and the admin is told); nothing wrong is saved.
- DOM parsing does not exist on purpose: there are no real pages to base selectors on.
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from src.services.twogis_links import BASE_URL

_CATALOG_HOST_RE = re.compile(r"^catalog\.api\.2gis\.(?:ru|com)$")
_ITEMS_PATH = "/3.0/items"
_CARD_PATH = "/3.0/items/byid"
# Signs of the captcha page, all taken from the real response.
_CAPTCHA_HOST = "captcha.2gis."
_CAPTCHA_TITLE = "captcha"
_CAPTCHA_HTML_MARKERS: tuple[str, ...] = ("g-recaptcha", "подозрительную активность", "что вы не робот")
_REFUSING_STATUSES: frozenset[int] = frozenset({401, 403, 429})
_SITE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*2gis\.(?:ru|com)$")
_FIRM_LINK_RE = re.compile(r"/firm/([0-9]+)")


def build_search_url(city_slug: str, text: str, page: int = 1) -> str:
    """Public search page: https://2gis.ru/spb/search/<text>[/page/<n>]."""
    url = f"{BASE_URL}/{city_slug}/search/{quote(text, safe='')}"
    return f"{url}/page/{page}" if page > 1 else url


def _is_catalog_url(url: str, path_check: str) -> bool:
    parts = urlsplit(url)
    return bool(_CATALOG_HOST_RE.match(parts.netloc)) and parts.path.rstrip("/") == path_check


def is_search_response(url: str) -> bool:
    """Is this response of the page's own scripts a list of organizations?"""
    return _is_catalog_url(url, _ITEMS_PATH)


def is_card_response(url: str) -> bool:
    """Is this response of the page's own scripts an organization card?"""
    return _is_catalog_url(url, _CARD_PATH)


def is_site_response(url: str) -> bool:
    """Does this response come from 2GIS at all? Only the diagnostics look wider than the catalog."""
    return bool(_SITE_HOST_RE.match(urlsplit(url).netloc))


def first_firm_number(html: str) -> str | None:
    """
    A link to some organization in the text of a page, only for the diagnostics to have a card to open.

    Nothing is ever read from the page this way, so this is not parsing of the markup.
    """
    match = _FIRM_LINK_RE.search(html)
    return match.group(1) if match else None


def detect_block(status: int | None, final_url: str, title: str, html: str) -> str | None:
    """Return why the site refused to show the page, or None if it did not."""
    if status in _REFUSING_STATUSES:
        return f"сайт ответил кодом {status}"
    if is_captcha(final_url, title, html):
        return "сайт показал капчу"
    return None


def is_captcha(final_url: str, title: str, html: str) -> bool:
    """Is this page the captcha of the site?"""
    if urlsplit(final_url).netloc.startswith(_CAPTCHA_HOST) or _CAPTCHA_TITLE in title.lower():
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _CAPTCHA_HTML_MARKERS)


@dataclass
class SearchData:
    """Items of a search page and whether the site has more of them."""

    items: list[dict[str, Any]]
    total: int | None


def _meta_code(body: dict[str, Any]) -> int | None:
    meta = body.get("meta")
    code = meta.get("code") if isinstance(meta, dict) else None
    return code if isinstance(code, int) else None


def read_search_bodies(bodies: list[dict[str, Any]]) -> SearchData | None:
    """
    Find the search result among the responses of a page.

    Returns None if nothing resembles one: the page is not what the code expects.
    'Not found' from the site is a normal empty result.
    """
    for body in bodies:
        result = body.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        if isinstance(items, list):
            total = result.get("total") if isinstance(result, dict) else None
            return SearchData(
                items=[item for item in items if isinstance(item, dict)],
                total=total if isinstance(total, int) else None,
            )
    if any(_meta_code(body) == 404 for body in bodies):
        return SearchData(items=[], total=0)
    return None


def refusal_in_bodies(bodies: list[dict[str, Any]]) -> str | None:
    """The page may load, while the requests of its scripts are refused."""
    for body in bodies:
        code = _meta_code(body)
        if code in _REFUSING_STATUSES:
            return f"сайт отказал в данных (код {code})"
    return None


def read_card_body(bodies: list[dict[str, Any]], firm_id: str) -> dict[str, Any] | None:
    """Find the card of this organization among the responses of a page."""
    numeric_id = firm_id.split("_", 1)[0]
    for body in bodies:
        result = body.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and str(item.get("id", "")).split("_", 1)[0] == numeric_id:
                return item
    return None
