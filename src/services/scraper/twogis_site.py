"""
Everything that depends on how 2gis.ru looks: URLs, signs of a block, the format of responses.

When the site changes, this is the file to edit.

STATUS OF THIS FILE (2026-11-06). Checked against one real search page and one card, saved with
collect_cli.py --assisted --check --save-sample:
- Pages arrive with their data in the variable initialState, which has the format of Catalog API 3.0:
  data.entity.profile.<id>.data is an organization, data.search.pagination.<search id>.<page>.data
  lists the organizations found. The separate responses of a page hold nothing about organizations.
- A search lists organizations without contact_groups: only a card has them, so absent contacts in
  a list say nothing. Phones are in the state of a card in full, no click is needed.
- Five more real cards were read the same way on the same day, four of them with a site, none of the
  addresses on 2GIS. Other outside links on those pages go through link.2gis.ru, so a site known only
  by such a link stays unread; whether cards with such a site exist was not seen.
- Separate responses and the links of a page are read only if there is no state.
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from src.services.checker import is_social_domain, url_host
from src.services.scraper.browser import PageSnapshot
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


# ---------- the state a page arrives with ----------


_STATE_VARIABLE: str = "initialState"
_INLINE_STATE_START: str = f"var {_STATE_VARIABLE} = JSON.parse('"
_JS_ESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)
_JS_ESCAPES: dict[str, str] = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_UNESCAPED_QUOTE_RE = re.compile(r"(?<!\\)(?:\\\\)*'")
_ANCHOR_HREF_RE = re.compile(r"<a\b[^>]*?\bhref=\"([^\"]+)\"", re.IGNORECASE)
# Marks a card put together from the links of a page: it cannot say that a site is not there.
FROM_LINKS_KEY: str = "contacts_from_links"
# Links of the page itself: its footer has them whatever the organization is.
_SITE_OWN_HOST_RE = re.compile(r"(?:^|\.)(?:2gis\.[a-z]{2,10}|2gis\.onelink\.me|otello\.ru)$", re.ASCII)


def _unescape_js(match: re.Match[str]) -> str:
    code = match.group(1)
    if code[0] in "ux" and len(code) > 1:
        return chr(int(code[1:], 16))
    return _JS_ESCAPES.get(code, code)


def inline_state(html: str) -> dict[str, Any] | None:
    """The state from the text of a page, for when the page did not let it be read directly."""
    start = html.find(_INLINE_STATE_START)
    if start < 0:
        return None
    start += len(_INLINE_STATE_START)
    # A quote in the text is escaped, and an escaped backslash before a quote does not count.
    end = _UNESCAPED_QUOTE_RE.search(html, start)
    if end is None:
        return None
    text = _JS_ESCAPE_RE.sub(_unescape_js, html[start : end.end() - 1])
    try:
        state = json.loads(text)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _page_state(snapshot: PageSnapshot) -> dict[str, Any] | None:
    state = snapshot.page_state.get(_STATE_VARIABLE)
    return state if isinstance(state, dict) else inline_state(snapshot.html)


def _dig(value: Any, *keys: str) -> Any:
    """What lies by these keys, None as soon as the structure is different."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _entity(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    data = _dig(state, "data", "entity", "profile", key, "data")
    return data if isinstance(data, dict) else None


def read_search_state(state: dict[str, Any]) -> SearchData | None:
    """The organizations a search page shows. None if it says nothing about a search."""
    profiles = _dig(state, "data", "search", "profile")
    pagination = _dig(state, "data", "search", "pagination")
    if not isinstance(profiles, dict) or not profiles or not isinstance(pagination, dict):
        return None
    search_id, profile = next(iter(profiles.items()))
    total = _dig(profile, "data", "total")
    pages = pagination.get(search_id)
    if not isinstance(pages, dict):
        return SearchData(items=[], total=0) if total == 0 else None
    items: list[dict[str, Any]] = []
    for page in pages.values():
        ids = page.get("data") if isinstance(page, dict) else None
        for firm_id in ids if isinstance(ids, list) else []:
            entity = _entity(state, str(firm_id))
            if entity is not None:
                items.append(entity)
    if not items and total != 0:
        return None
    return SearchData(items=items, total=total if isinstance(total, int) else None)


def read_card_state(state: dict[str, Any], firm_id: str) -> dict[str, Any] | None:
    """This organization in the state of a page. Another one in its place is not its card."""
    number = firm_id.split("_", 1)[0]
    profiles = _dig(state, "data", "entity", "profile")
    for entry in profiles.values() if isinstance(profiles, dict) else []:
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict) and str(data.get("id", "")).split("_", 1)[0] == number:
            return data
    return None


def read_card_links(snapshot: PageSnapshot, firm_id: str) -> dict[str, Any] | None:
    """
    A card from the links of a page: tel:, mailto: and outside links. The last resort, with no state.

    More than one outside address cannot be told apart, so then none is taken for the site.
    """
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


def read_search_page(snapshot: PageSnapshot) -> SearchData | None:
    """The result of a search, from wherever the page keeps it."""
    state = _page_state(snapshot)
    data = read_search_state(state) if state is not None else None
    return data if data is not None else read_search_bodies(snapshot.bodies_from_or_all(is_search_response))


def read_card_page(snapshot: PageSnapshot, firm_id: str) -> dict[str, Any] | None:
    """The card of an organization, from wherever the page keeps it."""
    state = _page_state(snapshot)
    card = read_card_state(state, firm_id) if state is not None else None
    if card is None:
        card = read_card_body(snapshot.bodies_from_or_all(is_card_response), firm_id)
    return card if card is not None else read_card_links(snapshot, firm_id)
