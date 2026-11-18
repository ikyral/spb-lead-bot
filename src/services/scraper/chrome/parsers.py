"""
2GIS Chrome DOM click/pagination/in-building/firm parsers and async session wrapper.

Every navigation and every click goes through a gate first: a callback of the provider that keeps
the limit of pages, checks robots.txt and makes the pause. The readers never decide that themselves.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import enum
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import SecretStr

from src.services.api_client import (
    MAX_RESPONSE_BYTES,
    NO_CONNECTION_TEXT,
    ProviderNoConnectionError,
    ProviderProxyError,
    ProviderStoppedError,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.scraper.snapshot import PageSnapshot

from .exceptions import ChromeException, ChromePathNotFound, ChromeRuntimeException, ProxyError
from .filters import IN_BUILDING_URL_PATTERN, ITEM_RESPONSE_PATTERN
from .options import ChromeOptions, ChromeParserOptions
from .remote import ChromeRemote

if TYPE_CHECKING:
    from pathlib import Path

    from .dom import DOMNode

logger = logging.getLogger(__name__)

BlockChecker = Callable[[PageSnapshot], str | None]
UrlKind = Literal["search", "in_building", "firm"]
GateKind = Literal["open", "page", "card"]
_T = TypeVar("_T")

_PAGE_SUFFIX_RE = re.compile(r"/page/\d+(?=/|$)")
_PAGE_NUMBER_RE = re.compile(r"/page/(\d+)(?=/|\?|#|$)")
_SEARCH_LINK_RE = re.compile(r"/(firm|station)/[^/?#]+.*[?&]stat=([^&#]+)")
_FIRM_PATH_RE = re.compile(r"/firm/[^/?#]+")
_FIRM_ID_RE = re.compile(r"/firm/([0-9]+)")
_IN_BUILDING_FIRM_RE = re.compile(r"/firm/[^/?#]+/?$")
_NO_RESULTS_TITLE_RE = re.compile(r"Ничего не найдено|Не найдено", re.IGNORECASE)
_IN_BUILDING_PATTERN = re.compile(IN_BUILDING_URL_PATTERN)

_MAX_CLICK_ATTEMPTS = 3
_STALE_DOM_TIMEOUT_SECONDS = 15.0
_SETTLE_POLL_SECONDS = 0.2
_NETWORK_ERROR_MARKERS: tuple[str, ...] = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT",
)
_FATAL_RUNTIME_MARKERS: tuple[str, ...] = (
    "Tab has been stopped",
    "CDP tab connection stopped",
    "окно браузера закрыто",
)


class GateDecision(enum.Enum):
    """What the provider allows: do it, leave out this one, or stop all actions of the run."""

    GO = "go"
    SKIP = "skip"
    STOP = "stop"


@dataclass(frozen=True)
class GateAction:
    """One action that reaches the site: opening a URL, clicking a pagination link or a card."""

    kind: GateKind
    url: str = ""
    page: int = 0
    firm_id: str = ""


Gate = Callable[[GateAction], GateDecision]


class _GateStopped(Exception):
    """The gate refused an action that the reading cannot do without."""


def _translate_chrome_error(exc: ChromeException) -> Exception:
    """Map low-level Chrome CDP exceptions to the provider error hierarchy."""
    if isinstance(exc, ProxyError):
        return ProviderProxyError(str(exc))
    if isinstance(exc, ChromePathNotFound):
        return ProviderUnavailableError(str(exc))
    if isinstance(exc, ChromeRuntimeException):
        return ProviderUnavailableError(str(exc))
    message = str(exc)
    if any(marker in message for marker in _NETWORK_ERROR_MARKERS):
        return ProviderNoConnectionError(NO_CONNECTION_TEXT)
    return ProviderTemporaryError(message or "ошибка Chrome CDP")


@dataclass(frozen=True)
class ScrapedSearchResult:
    """Result of scraping a single search or `/inside/` page via Chrome CDP."""

    snapshot: PageSnapshot
    items: list[dict[str, Any]]
    has_more: bool
    blocked_reason: str | None = None
    not_found: bool = False
    # The gate refused an action: reading ended early, `items` holds what was read before.
    stopped: bool = False
    # Links to organizations the page listed, and whether it said that nothing was found.
    links_found: int = 0
    nothing_found: bool = False


def strip_page_number(url: str) -> str:
    """Remove `/page/<n>` from a 2GIS URL path while preserving query and fragment."""
    parts = urlsplit(url)
    clean_path = _PAGE_SUFFIX_RE.sub("", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, clean_path or "/", parts.query, parts.fragment))


def classify_2gis_url(url: str) -> UrlKind:
    """Classify a 2GIS URL as `/inside/`, `/firm/`, or search listing."""
    if _IN_BUILDING_PATTERN.match(url):
        return "in_building"
    path = urlsplit(url).path
    if re.match(r"^(?:/[^/]+)?/firm/[^/?#]+", path):
        return "firm"
    return "search"


def firm_id_from_href(href: str) -> str:
    """The number of the organization in a card link, or '' if the link has none."""
    match = _FIRM_ID_RE.search(urlsplit(href).path)
    return match.group(1) if match else ""


def _is_valid_base64(value: str) -> bool:
    unquoted = unquote(value).strip()
    if not unquoted:
        return False
    padded = unquoted + "=" * (-len(unquoted) % 4)
    try:
        base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def is_search_result_link(node: DOMNode) -> bool:
    """Match sidebar firm or station links bearing a valid base64 `stat=` query parameter."""
    if node.local_name != "a":
        return False
    href = node.attributes.get("href", "")
    match = _SEARCH_LINK_RE.search(href)
    if match is None:
        return False
    return _is_valid_base64(match.group(2))


def is_in_building_firm_link(node: DOMNode) -> bool:
    """Match `/firm/<id>` links inside a 2GIS `/inside/` building listing."""
    if node.local_name != "a":
        return False
    href = node.attributes.get("href", "")
    path = urlsplit(href).path
    return bool(_IN_BUILDING_FIRM_RE.search(path))


def _extract_pagination_links(document: DOMNode, min_page_exclusive: int) -> list[tuple[int, DOMNode]]:
    """Return `(page_number, node)` pairs for pagination links beyond `min_page_exclusive`."""
    found: dict[int, DOMNode] = {}
    for node in document.search(lambda item: item.local_name == "a" and "/page/" in item.attributes.get("href", "")):
        href = node.attributes.get("href", "")
        match = _PAGE_NUMBER_RE.search(href)
        if match is None:
            continue
        page_num = int(match.group(1))
        if page_num > min_page_exclusive and page_num not in found:
            found[page_num] = node
    return sorted(found.items(), key=lambda pair: pair[0])


def _empty_snapshot() -> PageSnapshot:
    return PageSnapshot(status=None, final_url="", title="", html="")


def _snapshot_from_remote(
    remote: ChromeRemote,
    status: int | None,
    *,
    json_bodies: list[dict[str, Any]] | None = None,
    json_urls: list[str] | None = None,
    include_state: bool = True,
) -> PageSnapshot:
    """Read URL, title, HTML, and serialized global state from the active Chrome tab."""
    final_url, title, html = remote.read_current_page()
    if include_state:
        page_state, page_state_problems = remote.read_page_state()
    else:
        page_state, page_state_problems = {}, {}
    return PageSnapshot(
        status=status,
        final_url=final_url,
        title=title,
        html=html,
        json_bodies=json_bodies or [],
        json_urls=json_urls or [],
        page_state=page_state,
        page_state_problems=page_state_problems,
    )


def _check_snapshot_block(
    remote: ChromeRemote,
    status: int | None,
    block_checker: BlockChecker | None,
    *,
    include_state: bool = False,
) -> tuple[PageSnapshot, str | None]:
    """Read current page snapshot and check whether it indicates a block or error."""
    snapshot = _snapshot_from_remote(remote, status, include_state=include_state)
    if block_checker is not None:
        reason = block_checker(snapshot)
        if reason is not None:
            return snapshot, reason
    if status is not None and status < 0:
        return snapshot, "сетевая ошибка при загрузке страницы 2ГИС"
    if status in (401, 403, 429):
        return snapshot, f"2ГИС вернул HTTP {status}"
    return snapshot, None


def _collect_json_responses(
    remote: ChromeRemote,
    wants_body: Callable[[str], bool],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode JSON bodies from intercepted network responses matching `wants_body`."""
    json_bodies: list[dict[str, Any]] = []
    json_urls: list[str] = []
    for response in remote.get_responses():
        resp_url = str(response.get("url") or "")
        if not wants_body(resp_url):
            continue
        mime_type = str(response.get("mimeType") or "")
        if mime_type and "json" not in mime_type.lower():
            continue
        body_text = remote.get_response_body(response)
        if not body_text or len(body_text.encode("utf-8", errors="ignore")) > MAX_RESPONSE_BYTES:
            continue
        try:
            parsed = json.loads(body_text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            json_bodies.append(parsed)
            json_urls.append(resp_url)
    return json_bodies, json_urls


def _parse_byid_payload(raw_body: str, max_bytes: int = MAX_RESPONSE_BYTES) -> dict[str, Any] | None:
    """Validate and parse `/items/byid` JSON payload; returns full API envelope or None."""
    if not raw_body or len(raw_body.encode("utf-8", errors="ignore")) > max_bytes:
        return None
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    meta = parsed.get("meta")
    result = parsed.get("result")
    if not isinstance(meta, dict) or meta.get("code") != 200 or not isinstance(result, dict):
        return None
    items = result.get("items")
    if not isinstance(items, list) or not items:
        return None
    return parsed


class _BaseChromePageReader:
    """Shared navigation, page-ready wait, and card-click logic for 2GIS Chrome parsers."""

    def __init__(
        self,
        remote: ChromeRemote,
        parser_options: ChromeParserOptions,
        *,
        gate: Gate,
        page_timeout_seconds: float = 45.0,
    ) -> None:
        self._remote = remote
        self._options = parser_options
        self._gate = gate
        self._page_timeout_seconds = max(10.0, page_timeout_seconds)

    def _open(self, url: str) -> int | None:
        """Navigate to `url` if the gate allows it, clearing stale network records first."""
        if self._gate(GateAction("open", url=url)) is not GateDecision.GO:
            raise _GateStopped
        self._remote.clear_requests()
        self._remote.drain_responses(ITEM_RESPONSE_PATTERN)
        self._remote.navigate(url, timeout=int(self._page_timeout_seconds))
        doc_response = self._remote.get_document_response()
        if not isinstance(doc_response, dict):
            return None
        status = doc_response.get("status")
        if status == -1:
            status_text = str(doc_response.get("statusText") or "")
            if any(marker in status_text for marker in _NETWORK_ERROR_MARKERS):
                raise ChromeException(status_text)
        return int(status) if isinstance(status, int) else None

    def _wait_requests_finished(
        self,
        status: int | None,
        block_checker: BlockChecker | None,
        *,
        check_ready: Callable[[], bool] | None = None,
    ) -> tuple[PageSnapshot | None, str | None]:
        """
        Wait for document.readyState == 'complete', then for `check_ready` on the DOM, then give up.

        A sign of a block ends the wait at once.
        """
        deadline = time.monotonic() + self._page_timeout_seconds

        while time.monotonic() < deadline:
            snapshot, blocked_reason = _check_snapshot_block(
                self._remote,
                status,
                block_checker,
                include_state=False,
            )
            if blocked_reason is not None:
                return snapshot, blocked_reason

            ready_state = self._remote.execute_script("document.readyState")
            if ready_state == "complete" and (check_ready is None or check_ready()):
                return None, None
            time.sleep(_SETTLE_POLL_SECONDS)

        return None, None

    def _collect_items_from_links(
        self,
        find_links: Callable[[DOMNode], list[DOMNode]],
        *,
        status: int | None,
        block_checker: BlockChecker | None,
        skip_hrefs: set[str] | None = None,
        max_items: int | None = None,
        refetch_on_stale: bool = True,
    ) -> tuple[list[dict[str, Any]], list[str], set[str], str | None, bool]:
        """Click each matching link in DOM and collect `/items/byid` payloads. The last value: the gate stopped it."""
        document = self._remote.get_document(full=True)
        nodes = find_links(document)
        target_items: list[tuple[str, DOMNode]] = []
        seen_hrefs: set[str] = set()
        for node in nodes:
            href = node.attributes.get("href", "")
            if not href or href in seen_hrefs:
                continue
            if skip_hrefs and href in skip_hrefs:
                continue
            seen_hrefs.add(href)
            target_items.append((href, node))

        collected: list[dict[str, Any]] = []
        collected_urls: list[str] = []
        attempted_hrefs: set[str] = set()

        for href, node in target_items:
            if max_items is not None and len(collected) >= max_items:
                break

            def _refetch(target_href: str = href) -> DOMNode | None:
                if not refetch_on_stale:
                    return None
                return self._find_node_by_href(find_links, target_href)

            attempted_hrefs.add(href)
            try:
                payload, response_url, blocked_reason = self._click_single_href(
                    href=href,
                    initial_node=node,
                    find_node=_refetch,
                    status=status,
                    block_checker=block_checker,
                )
            except _GateStopped:
                return collected, collected_urls, attempted_hrefs, None, True

            if blocked_reason is not None:
                return collected, collected_urls, attempted_hrefs, blocked_reason, False
            if payload is not None:
                collected.append(payload)
                if response_url:
                    collected_urls.append(response_url)

        return collected, collected_urls, attempted_hrefs, None, False

    def _find_node_by_href(self, find_links: Callable[[DOMNode], list[DOMNode]], target_href: str) -> DOMNode | None:
        document = self._remote.get_document(full=True)
        for node in find_links(document):
            if node.attributes.get("href") == target_href:
                return node
        return None

    def _click_single_href(
        self,
        *,
        href: str,
        initial_node: DOMNode | None,
        find_node: Callable[[], DOMNode | None],
        status: int | None,
        block_checker: BlockChecker | None,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """
        Click one card link up to `_MAX_CLICK_ATTEMPTS` times and parse `/items/byid`.

        The gate is asked before every click: a card it leaves out is not clicked at all.
        """
        current_node = initial_node
        firm_id = firm_id_from_href(href)
        for attempt in range(_MAX_CLICK_ATTEMPTS):
            if current_node is None or attempt > 0:
                current_node = find_node()
            if current_node is None:
                return None, "", None

            decision = self._gate(GateAction("card", firm_id=firm_id))
            if decision is GateDecision.STOP:
                raise _GateStopped
            if decision is GateDecision.SKIP:
                return None, "", None

            self._remote.drain_responses(ITEM_RESPONSE_PATTERN)
            try:
                self._remote.perform_click(current_node)
            except ChromeRuntimeException as exc:
                if any(marker in str(exc) for marker in _FATAL_RUNTIME_MARKERS):
                    raise
                current_node = None
                continue

            response = self._remote.wait_response(ITEM_RESPONSE_PATTERN)
            if response is None:
                _, blocked_reason = _check_snapshot_block(
                    self._remote,
                    status,
                    block_checker,
                    include_state=False,
                )
                if blocked_reason is not None:
                    return None, "", blocked_reason
                continue

            resp_status = response.get("status")
            resp_url = str(response.get("url") or "")
            if resp_status == 404:
                return None, "", None
            if resp_status in (401, 403, 429) or (isinstance(resp_status, int) and resp_status < 0):
                _, blocked_reason = _check_snapshot_block(
                    self._remote,
                    int(resp_status),
                    block_checker,
                    include_state=False,
                )
                return None, "", blocked_reason or f"2ГИС вернул HTTP {resp_status} на карточке"

            raw_body = self._remote.get_response_body(response)
            payload = _parse_byid_payload(raw_body)
            if payload is not None:
                return payload, resp_url, None

            _, blocked_reason = _check_snapshot_block(self._remote, status, block_checker, include_state=False)
            if blocked_reason is not None:
                return None, "", blocked_reason

        logger.debug("Exhausted %d click attempts for %s without /items/byid payload", _MAX_CLICK_ATTEMPTS, href)
        return None, "", None


class SearchPageReader(_BaseChromePageReader):
    """Stateful step-by-step search listing reader ported from `parser-2gis-new` `MainParser`.

    Opens page 1 of a search directly, without `/page/<n>`, then walks pages sequentially by
    clicking DOM pagination links so page >= 6 never redirects to page 1.
    """

    def __init__(
        self,
        remote: ChromeRemote,
        parser_options: ChromeParserOptions,
        *,
        gate: Gate,
        page_timeout_seconds: float = 45.0,
    ) -> None:
        super().__init__(remote, parser_options, gate=gate, page_timeout_seconds=page_timeout_seconds)
        self._active_base_url: str | None = None
        self._current_page: int = 0
        self._visited_links: set[str] = set()

    def reset(self) -> None:
        """Reset active search state so the next call opens a fresh query."""
        self._active_base_url = None
        self._current_page = 0
        self._visited_links.clear()

    def read_page(
        self,
        url: str,
        page: int = 1,
        *,
        max_items: int | None = None,
        block_checker: BlockChecker | None = None,
        reset: bool = False,
        continue_current: bool = False,
    ) -> ScrapedSearchResult:
        """Read search page `page` for `url`, continuing in-place when sequential."""
        base_url = strip_page_number(url)
        same_current_page = continue_current and self._active_base_url == base_url and self._current_page == page
        needs_fresh_open = (
            reset
            or self._active_base_url != base_url
            or self._current_page < 1
            or (page <= self._current_page and not same_current_page)
        )

        if needs_fresh_open:
            try:
                result = self._open_first_page(base_url, block_checker=block_checker)
            except _GateStopped:
                return ScrapedSearchResult(snapshot=_empty_snapshot(), items=[], has_more=False, stopped=True)
            if result is not None:
                return result

        while self._current_page < page:
            advanced, blocked_result = self._advance_one_page(block_checker=block_checker)
            if blocked_result is not None:
                return blocked_result
            if not advanced:
                snapshot = _snapshot_from_remote(self._remote, status=200, include_state=True)
                return ScrapedSearchResult(snapshot=snapshot, items=[], has_more=False, stopped=True)

        return self._scrape_current_page(max_items=max_items, block_checker=block_checker)

    def _open_first_page(self, base_url: str, *, block_checker: BlockChecker | None) -> ScrapedSearchResult | None:
        self.reset()
        status = self._open(base_url)
        if self._options.skip_404_response and status == 404:
            snapshot = _snapshot_from_remote(self._remote, status=404, include_state=True)
            return ScrapedSearchResult(snapshot=snapshot, items=[], has_more=False, not_found=True)

        blocked_snapshot, blocked_reason = self._wait_requests_finished(
            status,
            block_checker,
            check_ready=self._page_has_search_links_or_empty,
        )
        if blocked_reason is not None and blocked_snapshot is not None:
            return ScrapedSearchResult(
                snapshot=blocked_snapshot,
                items=[],
                has_more=False,
                blocked_reason=blocked_reason,
            )

        self._active_base_url = base_url
        self._current_page = 1
        return None

    def _page_has_search_links_or_empty(self) -> bool:
        try:
            doc = self._remote.get_document(full=True)
        except ChromeException:
            return False
        if doc.search(is_search_result_link):
            return True
        _, title, _ = self._remote.read_current_page()
        return bool(_NO_RESULTS_TITLE_RE.search(title))

    def _find_search_links(self, document: DOMNode) -> list[DOMNode]:
        return document.search(is_search_result_link)

    def _all_search_hrefs(self) -> set[str]:
        document = self._remote.get_document(full=True)
        return {node.attributes["href"] for node in self._find_search_links(document) if "href" in node.attributes}

    def _wait_new_links_loaded(self, block_checker: BlockChecker | None) -> tuple[PageSnapshot | None, str | None]:
        """Wait until pagination DOM refreshes so links differ from `self._visited_links`."""
        deadline = time.monotonic() + _STALE_DOM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            blocked_snapshot, blocked_reason = self._wait_requests_finished(
                status=200,
                block_checker=block_checker,
                check_ready=self._page_has_search_links_or_empty,
            )
            if blocked_reason is not None:
                return blocked_snapshot, blocked_reason

            hrefs = self._all_search_hrefs()
            if hrefs and not hrefs.issubset(self._visited_links):
                return None, None
            if not hrefs:
                _, title, _ = self._remote.read_current_page()
                if _NO_RESULTS_TITLE_RE.search(title):
                    return None, None
            time.sleep(_SETTLE_POLL_SECONDS)
        return None, None

    def _advance_one_page(self, *, block_checker: BlockChecker | None) -> tuple[bool, ScrapedSearchResult | None]:
        document = self._remote.get_document(full=True)
        current_hrefs = {
            node.attributes["href"] for node in self._find_search_links(document) if "href" in node.attributes
        }
        self._visited_links.update(current_hrefs)

        pages_ahead = _extract_pagination_links(document, self._current_page)
        if not pages_ahead:
            return False, None

        next_page_num, next_page_node = pages_ahead[0]
        if self._gate(GateAction("page", page=next_page_num)) is not GateDecision.GO:
            return False, None
        self._remote.clear_requests()
        self._remote.drain_responses(ITEM_RESPONSE_PATTERN)
        try:
            self._remote.perform_click(next_page_node)
        except ChromeRuntimeException as exc:
            if any(marker in str(exc) for marker in _FATAL_RUNTIME_MARKERS):
                raise
            return False, None

        blocked_snapshot, blocked_reason = self._wait_new_links_loaded(block_checker)
        if blocked_reason is not None and blocked_snapshot is not None:
            return False, ScrapedSearchResult(
                snapshot=blocked_snapshot,
                items=[],
                has_more=False,
                blocked_reason=blocked_reason,
            )

        self._current_page = next_page_num
        return True, None

    def _scrape_current_page(
        self,
        *,
        max_items: int | None,
        block_checker: BlockChecker | None,
    ) -> ScrapedSearchResult:
        document = self._remote.get_document(full=True)
        all_nodes = self._find_search_links(document)
        all_hrefs = {node.attributes["href"] for node in all_nodes if "href" in node.attributes}
        pages_ahead = _extract_pagination_links(document, self._current_page)
        has_more = bool(pages_ahead)

        def _firm_links_only(doc: DOMNode) -> list[DOMNode]:
            return [
                node
                for node in doc.search(is_search_result_link)
                if _FIRM_PATH_RE.search(node.attributes.get("href", ""))
            ]

        items, item_urls, _attempted, blocked_reason, stopped = self._collect_items_from_links(
            _firm_links_only,
            status=200,
            block_checker=block_checker,
            max_items=max_items,
            refetch_on_stale=True,
        )
        self._visited_links.update(all_hrefs)

        if self._options.use_gc and self._current_page % self._options.gc_pages_interval == 0:
            self._remote.execute_script("window.gc && window.gc()")

        if not items and blocked_reason is None and not stopped:
            items, item_urls = _collect_json_responses(
                self._remote,
                lambda resp_url: "api.2gis." in resp_url,
            )

        self._remote.clear_requests()
        snapshot = _snapshot_from_remote(
            self._remote,
            status=200,
            json_bodies=items,
            json_urls=item_urls,
            include_state=True,
        )
        return ScrapedSearchResult(
            snapshot=snapshot,
            items=items,
            has_more=False if blocked_reason is not None or stopped else has_more,
            blocked_reason=blocked_reason,
            stopped=stopped,
            links_found=len(all_hrefs),
            nothing_found=not all_hrefs and bool(_NO_RESULTS_TITLE_RE.search(snapshot.title)),
        )


class InBuildingPageReader(_BaseChromePageReader):
    """Parser for `/inside/<building_id>` building directories from `parser-2gis-new`."""

    def read_page(
        self,
        url: str,
        *,
        max_items: int | None = None,
        block_checker: BlockChecker | None = None,
    ) -> ScrapedSearchResult:
        """Open `/inside/` URL and lazily click `/firm/<id>` links until exhausted."""
        try:
            status = self._open(url)
        except _GateStopped:
            return ScrapedSearchResult(snapshot=_empty_snapshot(), items=[], has_more=False, stopped=True)
        if self._options.skip_404_response and status == 404:
            snapshot = _snapshot_from_remote(self._remote, status=404, include_state=True)
            return ScrapedSearchResult(snapshot=snapshot, items=[], has_more=False, not_found=True)

        blocked_snapshot, blocked_reason = self._wait_requests_finished(status, block_checker)
        if blocked_reason is not None and blocked_snapshot is not None:
            return ScrapedSearchResult(
                snapshot=blocked_snapshot,
                items=[],
                has_more=False,
                blocked_reason=blocked_reason,
            )

        collected: list[dict[str, Any]] = []
        collected_urls: list[str] = []
        seen_hrefs: set[str] = set()
        stopped = False

        while max_items is None or len(collected) < max_items:
            remaining = None if max_items is None else max_items - len(collected)
            batch, batch_urls, attempted_hrefs, batch_block, stopped = self._collect_items_from_links(
                lambda doc: doc.search(is_in_building_firm_link),
                status=status,
                block_checker=block_checker,
                skip_hrefs=seen_hrefs,
                max_items=remaining,
                refetch_on_stale=True,
            )
            seen_hrefs.update(attempted_hrefs)
            collected.extend(batch)
            collected_urls.extend(batch_urls)

            if batch_block is not None:
                snapshot = _snapshot_from_remote(
                    self._remote,
                    status=status,
                    json_bodies=collected,
                    json_urls=collected_urls,
                    include_state=True,
                )
                return ScrapedSearchResult(
                    snapshot=snapshot,
                    items=collected,
                    has_more=False,
                    blocked_reason=batch_block,
                )
            if stopped:
                break

            doc_after = self._remote.get_document(full=True)
            remaining_new = {
                node.attributes["href"]
                for node in doc_after.search(is_in_building_firm_link)
                if "href" in node.attributes
            } - seen_hrefs
            if not remaining_new:
                break

        if self._options.use_gc:
            self._remote.execute_script("window.gc && window.gc()")

        self._remote.clear_requests()
        snapshot = _snapshot_from_remote(
            self._remote,
            status=status,
            json_bodies=collected,
            json_urls=collected_urls,
            include_state=True,
        )
        return ScrapedSearchResult(snapshot=snapshot, items=collected, has_more=False, stopped=stopped)


class ChromeScraperSession:
    """Async session wrapping Chrome CDP + `SearchPageReader` + `InBuildingPageReader` + direct `Firm` reads.

    `gate` is called in the worker thread before every navigation and click.
    """

    def __init__(
        self,
        *,
        gate: Gate,
        page_timeout_seconds: float = 45.0,
        profile_dir: Path | None = None,
        chrome_path: Path | None = None,
        silent_browser: bool = True,
        use_gc: bool = False,
        remote_factory: Callable[[ChromeOptions, list[str]], ChromeRemote] | None = None,
        proxy: SecretStr | None = None,
        proxy_country: str = "OFF",
        timezone: str | None = None,
        lang: str | None = None,
        headless: bool = False,
        memory_limit_mb: int = 0,
        light: bool = False,
        restart_every_cards: int = 0,
    ) -> None:
        self._gate = gate
        self._page_timeout_seconds = page_timeout_seconds
        self._profile_dir = profile_dir
        self._chrome_options = ChromeOptions(
            binary_path=chrome_path or None,
            profile_dir=profile_dir,
            start_maximized=True,
            silent_browser=silent_browser,
            headless=headless,
            light=light,
            proxy=proxy,
            proxy_country=proxy_country,
            timezone=timezone,
            lang=lang,
        )
        if memory_limit_mb > 0:
            self._chrome_options.memory_limit = memory_limit_mb
        # Chrome is started anew after so many read cards (its memory grows with every page); 0 = never.
        self._restart_every_cards = restart_every_cards
        self._cards_since_start = 0
        self._parser_options = ChromeParserOptions(use_gc=use_gc)
        self._remote_factory = remote_factory or ChromeRemote
        self._remote: ChromeRemote | None = None
        self._search_reader: SearchPageReader | None = None
        self._in_building_reader: InBuildingPageReader | None = None
        self._lock = asyncio.Lock()

    async def _run_locked(self, fn: Callable[..., _T], *args: Any) -> _T:
        async with self._lock:
            try:
                return await asyncio.to_thread(fn, *args)
            except ChromeException as exc:
                raise _translate_chrome_error(exc) from None
            except ImportError as exc:
                raise ProviderUnavailableError(f"не установлена зависимость для Chrome CDP: {exc}") from None

    async def start(self) -> None:
        """Launch Chrome with a visible window and attach to it over CDP."""
        await self._run_locked(self._start_sync)

    def _start_sync(self) -> None:
        if self._remote is not None:
            return
        remote = self._remote_factory(self._chrome_options, [ITEM_RESPONSE_PATTERN])
        remote.start()
        self._remote = remote
        self._cards_since_start = 0
        self._search_reader = SearchPageReader(
            remote,
            self._parser_options,
            gate=self._gate,
            page_timeout_seconds=self._page_timeout_seconds,
        )
        self._in_building_reader = InBuildingPageReader(
            remote,
            self._parser_options,
            gate=self._gate,
            page_timeout_seconds=self._page_timeout_seconds,
        )

    def _restart_sync(self) -> None:
        """Close Chrome and start it again. The profile stays, so the cookies stay too."""
        logger.info("Chrome is started anew after %d cards", self._cards_since_start)
        remote, self._remote = self._remote, None
        self._search_reader = None
        self._in_building_reader = None
        if remote is not None:
            remote.stop()
        self._start_sync()

    def _require_remote(self) -> ChromeRemote:
        if self._remote is None:
            raise ChromeRuntimeException("ChromeScraperSession не запущена")
        return self._remote

    async def visit(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot:
        """Open URL directly (used for `/firm/` cards and page inspection) and wait for the page to load."""
        return await self._run_locked(self._visit_sync, url, wants_body)

    def _visit_sync(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot:
        remote = self._require_remote()
        if self._search_reader is not None:
            self._search_reader.reset()

        reader = _BaseChromePageReader(
            remote,
            self._parser_options,
            gate=self._gate,
            page_timeout_seconds=self._page_timeout_seconds,
        )
        try:
            status = reader._open(url)
        except _GateStopped:
            raise ProviderStoppedError("ограничения запуска не разрешают открыть страницу") from None
        if status == 404:
            return _snapshot_from_remote(remote, status=404, include_state=True)

        reader._wait_requests_finished(status, block_checker=None)
        json_bodies, json_urls = _collect_json_responses(remote, wants_body)

        return _snapshot_from_remote(
            remote,
            status=status,
            json_bodies=json_bodies,
            json_urls=json_urls,
            include_state=True,
        )

    async def read_search(
        self,
        url: str,
        page: int = 1,
        *,
        max_items: int | None = None,
        block_checker: BlockChecker | None = None,
        reset: bool = False,
        continue_current: bool = False,
    ) -> ScrapedSearchResult:
        """Scrape a search or `/inside/` listing via `SearchPageReader` / `InBuildingPageReader`."""
        return await self._run_locked(
            self._read_search_sync,
            url,
            page,
            max_items,
            block_checker,
            reset,
            continue_current,
        )

    def _read_search_sync(
        self,
        url: str,
        page: int,
        max_items: int | None,
        block_checker: BlockChecker | None,
        reset: bool,
        continue_current: bool,
    ) -> ScrapedSearchResult:
        self._require_remote()
        kind = classify_2gis_url(url)
        if kind == "in_building":
            if self._search_reader is not None:
                self._search_reader.reset()
            if self._in_building_reader is None:
                raise ChromeRuntimeException("ChromeScraperSession не запущена")
            return self._in_building_reader.read_page(url, max_items=max_items, block_checker=block_checker)

        if self._search_reader is None:
            raise ChromeRuntimeException("ChromeScraperSession не запущена")
        if self._restart_every_cards and self._cards_since_start >= self._restart_every_cards:
            # The reader of the new browser opens the search page anew and walks to the asked page.
            self._restart_sync()
            if self._search_reader is None:
                raise ChromeRuntimeException("ChromeScraperSession не запущена")
        result = self._search_reader.read_page(
            url,
            page=page,
            max_items=max_items,
            block_checker=block_checker,
            reset=reset,
            continue_current=continue_current,
        )
        self._cards_since_start += len(result.items)
        return result

    async def read_page(self) -> PageSnapshot | None:
        """Read current page URL/title/HTML/state without navigating."""
        return await self._run_locked(self._read_page_sync)

    def _read_page_sync(self) -> PageSnapshot | None:
        if self._remote is None:
            return None
        return _snapshot_from_remote(self._remote, status=None, include_state=True)

    async def bring_to_front(self) -> None:
        """Bring Chrome window to front."""
        await self._run_locked(self._bring_to_front_sync)

    def _bring_to_front_sync(self) -> None:
        if self._remote is not None:
            self._remote.bring_to_front()

    async def close(self) -> None:
        """Stop Chrome remote debugging connection and Chrome process."""
        async with self._lock:
            remote, self._remote = self._remote, None
            self._search_reader = None
            self._in_building_reader = None
            if remote is not None:
                await asyncio.to_thread(remote.stop)
