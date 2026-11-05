"""
Everything that depends on how 2gis.ru looks: URLs, signs of a block, the format of responses.

When the site changes, this is the file to edit.
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

BASE_URL: str = "https://2gis.ru"

_CATALOG_HOST_RE = re.compile(r"^catalog\.api\.2gis\.(?:ru|com)$")
_ITEMS_PATH = "/3.0/items"
_CARD_PATH = "/3.0/items/byid"
# Signs of the captcha page, all taken from the real response.
_CAPTCHA_HOST = "captcha.2gis."
_CAPTCHA_TITLE = "captcha"
_CAPTCHA_HTML_MARKERS: tuple[str, ...] = ("g-recaptcha", "подозрительную активность", "что вы не робот")
_REFUSING_STATUSES: frozenset[int] = frozenset({401, 403, 429})

_INITIAL_STATE_RE = re.compile(r"var\s+initialState\s*=\s*JSON\.parse\(\s*('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")\s*\)")


def build_search_url(city_slug: str, text: str, page: int = 1) -> str:
    """Public search page: https://2gis.ru/spb/search/<text>[/page/<n>]."""
    url = f"{BASE_URL}/{city_slug}/search/{quote(text, safe='')}"
    return f"{url}/page/{page}" if page > 1 else url


def build_card_url(city_slug: str, firm_id: str) -> str:
    """Public organization card. Ids may have a hash suffix; the URL uses the numeric part."""
    return f"{BASE_URL}/{city_slug}/firm/{firm_id.split('_', 1)[0]}"


def _is_catalog_url(url: str, path_check: str) -> bool:
    parts = urlsplit(url)
    return bool(_CATALOG_HOST_RE.match(parts.netloc)) and parts.path.rstrip("/") == path_check


def is_search_response(url: str) -> bool:
    """Is this response of the page's own scripts a list of organizations?"""
    return _is_catalog_url(url, _ITEMS_PATH)


def is_card_response(url: str) -> bool:
    """Is this response of the page's own scripts an organization card?"""
    return _is_catalog_url(url, _CARD_PATH)


def detect_block(status: int | None, final_url: str, title: str, html: str) -> str | None:
    """Return why the site refused to show the page, or None if it did not."""
    if status in _REFUSING_STATUSES:
        return f"сайт ответил кодом {status}"
    if urlsplit(final_url).netloc.startswith(_CAPTCHA_HOST) or _CAPTCHA_TITLE in title.lower():
        return "сайт показал капчу"
    lowered = html.lower()
    if any(marker in lowered for marker in _CAPTCHA_HTML_MARKERS):
        return "сайт показал капчу"
    return None


@dataclass
class SearchData:
    """Items of a search page and whether the site has more of them."""

    items: list[dict[str, Any]]
    total: int | None


def _meta_code(body: dict[str, Any]) -> int | None:
    meta = body.get("meta")
    code = meta.get("code") if isinstance(meta, dict) else None
    return code if isinstance(code, int) else None


def _extract_initial_state(html: str) -> dict[str, Any] | None:
    """Extract SSR `initialState` JSON embedded in 2GIS HTML when XHR is not fired on initial load."""
    if not html:
        return None
    match = _INITIAL_STATE_RE.search(html)
    if not match:
        return None
    raw_literal = match.group(1)
    try:
        if raw_literal.startswith("'"):
            unescaped = raw_literal[1:-1].replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
        else:
            unescaped = json.loads(raw_literal)
        parsed = json.loads(unescaped)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def read_search_bodies(bodies: list[dict[str, Any]], html: str = "") -> SearchData | None:
    """
    Find the search result among the responses of a page (or fallback to SSR initialState in HTML).

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

    state = _extract_initial_state(html)
    if state is not None:
        profile_map = state.get("data", {}).get("entity", {}).get("profile", {})
        if isinstance(profile_map, dict) and profile_map:
            items = [
                entry["data"]
                for entry in profile_map.values()
                if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
            ]
            if items:
                return SearchData(items=items, total=None)
    return None


def refusal_in_bodies(bodies: list[dict[str, Any]]) -> str | None:
    """The page may load, while the requests of its scripts are refused."""
    for body in bodies:
        code = _meta_code(body)
        if code in _REFUSING_STATUSES:
            return f"сайт отказал в данных (код {code})"
    return None


def read_card_body(bodies: list[dict[str, Any]], firm_id: str, html: str = "") -> dict[str, Any] | None:
    """Find the card of this organization among the responses of a page (or SSR HTML)."""
    numeric_id = firm_id.split("_", 1)[0]
    for body in bodies:
        result = body.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and str(item.get("id", "")).split("_", 1)[0] == numeric_id:
                return item

    state = _extract_initial_state(html)
    if state is not None:
        profile_map = state.get("data", {}).get("entity", {}).get("profile", {})
        if isinstance(profile_map, dict):
            for entry in profile_map.values():
                item = entry.get("data") if isinstance(entry, dict) else None
                if isinstance(item, dict) and str(item.get("id", "")).split("_", 1)[0] == numeric_id:
                    return item
    return None
