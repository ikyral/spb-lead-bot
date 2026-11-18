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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeVar
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
# After so many cards in a row that could not be read the page is left: the rest would only spend the budget.
_MAX_UNREAD_IN_A_ROW = 3
_STALE_DOM_TIMEOUT_SECONDS = 15.0
# The list of results is redrawn for a while after the page is loaded (2GIS sorts it again for the area of its map):
# the clicks wait until it has not changed this many reads in a row, but not longer than the limit.
_LINKS_STABLE_READS = 2
_LINKS_POLL_SECONDS = 1.0
_LINKS_SETTLE_TIMEOUT_SECONDS = 30.0
# Snapshots of a diagnostic run: only the latest ones are kept, the disk of a small server is nearly full.
_SNAPSHOTS_KEPT = 3
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
    # Reading was stopped because several cards in a row could not be read; the text says why. Not a block.
    unreadable: str | None = None


class CollectedCards(NamedTuple):
    """What clicking the cards of one page gave."""

    items: list[dict[str, Any]]
    urls: list[str]
    attempted: set[str]
    blocked_reason: str | None
    stopped: bool
    unreadable: str | None = None


def describe_link(href: str) -> str:
    """What a link of a result list is, by its kind and number only: 'firm 7000...', 'station 9', 'other /path'."""
    path = urlsplit(href).path
    match = re.search(r"/(firm|station)/([^/?#]+)", path)
    if match is not None:
        return f"{match.group(1)} {match.group(2)}"
    return f"other {path[:40]}"


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
        snapshot_dir: Path | None = None,
    ) -> None:
        self._remote = remote
        self._options = parser_options
        self._gate = gate
        self._page_timeout_seconds = max(10.0, page_timeout_seconds)
        # For a diagnostic run: every search page is saved here, to see what the code saw.
        self._snapshot_dir = snapshot_dir
        # How long the reads of the whole page took (a slow read says that the machine is short of memory), and what
        # links to organizations the last read showed.
        self._document_seconds: list[float] = []
        self._lookup_seconds: list[float] = []
        self._last_document_seconds = 0.0
        self._last_firm_hrefs: list[str] = []

    def _read_document(self) -> DOMNode:
        """The whole tree of the page, with the time it took and the links to organizations it showed."""
        started = time.monotonic()
        document = self._remote.get_document(full=True)
        self._last_document_seconds = time.monotonic() - started
        self._document_seconds.append(self._last_document_seconds)
        self._last_firm_hrefs = [
            node.attributes.get("href", "")
            for node in document.search(is_search_result_link)
            if _FIRM_PATH_RE.search(node.attributes.get("href", ""))
        ]
        return document

    def _log_reading_time(self) -> None:
        """One line for a page: how many reads of the whole page, how long they took, and the quick searches."""
        seconds = self._document_seconds
        if seconds:
            logger.info(
                "Reads of the whole page: %d, the shortest %.1f s, the longest %.1f s, %.1f s in all; "
                "quick searches of a link: %d, %.1f s in all",
                len(seconds),
                min(seconds),
                max(seconds),
                sum(seconds),
                len(self._lookup_seconds),
                sum(self._lookup_seconds),
            )

    def _log_page_state(self, why: str) -> None:
        """What the page looks like now: the address, the links the last read showed, and how slow the reads were."""
        try:
            raw = self._remote.execute_script("String(location.href)")
        except ChromeException:
            raw = None
        url = str(raw or "")
        parts = urlsplit(url)
        logger.warning(
            "Page state (%s): address %s; /firm/ in the address: %s; map position (m=) in the address: %s; "
            "%d links to organizations in the last read of the page, the first ones: %s; "
            "the last read of the whole page took %.1f s",
            why,
            unquote(url) or "unknown",
            "/firm/" in parts.path,
            "m=" in parts.query,
            len(self._last_firm_hrefs),
            [describe_link(href) for href in self._last_firm_hrefs[:3]],
            self._last_document_seconds,
        )

    def _write_snapshot(self, prefix: str, extra: dict[str, Any], suffix: str = "") -> None:
        """The page (HTML) and a JSON next to it in the folder of a diagnostic run; only the latest ones are kept."""
        directory = self._snapshot_dir
        if directory is None:
            return
        try:
            url, title, html = self._remote.read_current_page()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # a name of a file, the local time
            stem = directory / f"{prefix}_{stamp}{suffix}"
            stem.with_suffix(".html").write_text(html, encoding="utf-8")
            summary = {"url": url, "title": title, **extra}
            stem.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Snapshot of the page saved: %s", stem)
            self._prune_snapshots(directory)
        except (OSError, ChromeException):
            logger.warning("Could not save the snapshot of the page", exc_info=True)

    @staticmethod
    def _prune_snapshots(directory: Path) -> None:
        """Delete all but the latest `_SNAPSHOTS_KEPT` snapshots (HTML with its JSON)."""
        pages = sorted(directory.glob("*.html"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        for old in pages[:-_SNAPSHOTS_KEPT]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)

    def _save_failure_snapshot(self, reason: str) -> None:
        links = [describe_link(href) for href in self._last_firm_hrefs]
        self._write_snapshot("failure", {"reason": reason, "links_in_the_last_read": links})

    def _open(self, url: str) -> int | None:
        """Navigate to `url` if the gate allows it, clearing stale network records first."""
        if self._gate(GateAction("open", url=url)) is not GateDecision.GO:
            raise _GateStopped
        self._remote.clear_requests()
        self._remote.drain_responses(ITEM_RESPONSE_PATTERN)
        self._remote.navigate(url, timeout=int(self._page_timeout_seconds))
        doc_response = self._remote.get_document_response()
        if not isinstance(doc_response, dict):
            logger.info("Page opened: %s, no answer recorded", unquote(url))
            return None
        status = doc_response.get("status")
        logger.info("Page opened: %s, status %s", unquote(url), status)
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
    ) -> CollectedCards:
        """
        Click each matching link in DOM and collect `/items/byid` payloads.

        A card that could not be read after its attempts is left and the next one is tried; after
        `_MAX_UNREAD_IN_A_ROW` such cards in a row the page is left (`unreadable`), so the rest of the budget is kept.
        """
        document = self._read_document()
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
        unread_in_a_row = 0
        total = len(target_items)

        for position, (href, node) in enumerate(target_items, start=1):
            if max_items is not None and len(collected) >= max_items:
                break

            def _refetch(target_href: str = href, initial: DOMNode = node) -> DOMNode | None:
                if not refetch_on_stale:
                    return initial
                return self._find_node_by_href(find_links, target_href)

            attempted_hrefs.add(href)
            try:
                payload, response_url, blocked_reason, unread = self._click_single_href(
                    href=href,
                    initial_node=node,
                    find_node=_refetch,
                    status=status,
                    block_checker=block_checker,
                    position=position,
                    total=total,
                )
            except _GateStopped:
                return CollectedCards(collected, collected_urls, attempted_hrefs, None, True)

            if blocked_reason is not None:
                return CollectedCards(collected, collected_urls, attempted_hrefs, blocked_reason, False)
            if payload is not None:
                collected.append(payload)
                unread_in_a_row = 0
                if response_url:
                    collected_urls.append(response_url)
            elif unread is not None:
                unread_in_a_row += 1
                if unread_in_a_row >= _MAX_UNREAD_IN_A_ROW:
                    reason = (
                        f"{unread_in_a_row} карточки подряд не прочитались ({unread}): "
                        "страницу или код надо проверить, это не блокировка"
                    )
                    logger.warning("Search page left: %s", reason)
                    self._log_page_state("several cards in a row could not be read")
                    self._save_failure_snapshot(reason)
                    return CollectedCards(collected, collected_urls, attempted_hrefs, None, False, reason)

        return CollectedCards(collected, collected_urls, attempted_hrefs, None, False)

    def _find_node_by_href(self, find_links: Callable[[DOMNode], list[DOMNode]], target_href: str) -> DOMNode | None:
        # The browser finds a link by its address at once; reading the whole page for every click is slow and heavy.
        finder = getattr(self._remote, "find_link_by_href", None)
        if finder is not None:
            started = time.monotonic()
            try:
                found = finder(target_href)
            except ChromeException as exc:
                logger.info("Quick search of the link failed (%s): the whole page is read instead", exc)
            else:
                self._lookup_seconds.append(time.monotonic() - started)
                if found is not None:
                    return found
                # Not found: the whole page is read to be sure, and it tells what the page looks like now.
        document = self._read_document()
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
        position: int = 0,
        total: int = 0,
    ) -> tuple[dict[str, Any] | None, str, str | None, str | None]:
        """
        Click one card link up to `_MAX_CLICK_ATTEMPTS` times and parse `/items/byid`.

        Returns (payload, its url, the reason of a block, the reason the card could not be read). The last one is
        set only when the card was tried and gave nothing: a card the gate leaves out or that is gone (404) is not that.
        The gate is asked before every click: a card it leaves out is not clicked at all.
        """
        current_node = initial_node
        firm_id = firm_id_from_href(href)
        label = f"Card {position}/{total} ({describe_link(href)})"
        failure = "no attempt was made"
        for attempt in range(_MAX_CLICK_ATTEMPTS):
            # The node is taken anew right before the click: the page may have been redrawn since the list was read.
            current_node = find_node()
            if current_node is None:
                logger.info("%s: the link is no longer on the page", label)
                self._log_page_state("the link is no longer on the page")
                return None, "", None, "the link is no longer on the page"

            decision = self._gate(GateAction("card", firm_id=firm_id))
            if decision is GateDecision.STOP:
                raise _GateStopped
            if decision is GateDecision.SKIP:
                logger.info("%s: left out by the gate", label)
                return None, "", None, None

            self._remote.drain_responses(ITEM_RESPONSE_PATTERN)
            try:
                self._remote.perform_click(current_node)
            except ChromeRuntimeException as exc:
                if any(marker in str(exc) for marker in _FATAL_RUNTIME_MARKERS):
                    raise
                failure = f"the link went stale on the page ({str(exc)[:60]})"
                logger.info("%s, attempt %d/%d: %s", label, attempt + 1, _MAX_CLICK_ATTEMPTS, failure)
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
                    return None, "", blocked_reason, None
                failure = "the catalog did not answer after the click"
                logger.info("%s, attempt %d/%d: %s", label, attempt + 1, _MAX_CLICK_ATTEMPTS, failure)
                continue

            resp_status = response.get("status")
            resp_url = str(response.get("url") or "")
            if resp_status == 404:
                logger.info("%s: the catalog answered 404, the card is left out", label)
                return None, "", None, None
            if resp_status in (401, 403, 429) or (isinstance(resp_status, int) and resp_status < 0):
                _, blocked_reason = _check_snapshot_block(
                    self._remote,
                    int(resp_status),
                    block_checker,
                    include_state=False,
                )
                return None, "", blocked_reason or f"2ГИС вернул HTTP {resp_status} на карточке", None

            raw_body = self._remote.get_response_body(response)
            payload = _parse_byid_payload(raw_body)
            if payload is not None:
                logger.info("%s: read", label)
                return payload, resp_url, None, None

            _, blocked_reason = _check_snapshot_block(self._remote, status, block_checker, include_state=False)
            if blocked_reason is not None:
                return None, "", blocked_reason, None
            failure = "the answer of the catalog is not a card"
            logger.info("%s, attempt %d/%d: %s", label, attempt + 1, _MAX_CLICK_ATTEMPTS, failure)

        logger.info("%s: not read after %d attempts (%s), the next card is tried", label, _MAX_CLICK_ATTEMPTS, failure)
        return None, "", None, failure


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
        snapshot_dir: Path | None = None,
    ) -> None:
        super().__init__(
            remote, parser_options, gate=gate, page_timeout_seconds=page_timeout_seconds, snapshot_dir=snapshot_dir
        )
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

    def _wait_for_stable_links(self) -> None:
        """
        Wait until the links to organizations are the same, in the same order, `_LINKS_STABLE_READS` reads in a row.

        That is at least three reads, however fast. Not longer than `_LINKS_SETTLE_TIMEOUT_SECONDS`: after that the
        page is read as it is. On a machine short of memory one read takes seconds, so the count matters as much as
        the time: both are written to the log.
        """
        started = time.monotonic()
        deadline = started + _LINKS_SETTLE_TIMEOUT_SECONDS
        previous: tuple[str, ...] | None = None
        same_in_a_row = 0
        reads = 0
        while True:
            self._read_document()
            reads += 1
            current = tuple(self._last_firm_hrefs)
            same_in_a_row = same_in_a_row + 1 if current == previous else 0
            previous = current
            if same_in_a_row >= _LINKS_STABLE_READS:
                logger.info(
                    "The list of results has settled: the same %d reads in a row, %d reads in all, %.1f s, %d links",
                    same_in_a_row + 1,
                    reads,
                    time.monotonic() - started,
                    len(current),
                )
                return
            if time.monotonic() >= deadline:
                logger.info(
                    "The list of results was still changing after %d reads, %.1f s: it is read as it is",
                    reads,
                    time.monotonic() - started,
                )
                return
            time.sleep(_LINKS_POLL_SECONDS)

    def _report_page(self, firm_links: list[DOMNode], all_links: list[DOMNode], pagination: int) -> None:
        """Say in the log what the page offers, and save it for a diagnostic run: the links are only listed by kind."""
        firm_hrefs = list(dict.fromkeys(node.attributes.get("href", "") for node in firm_links))
        other = len({node.attributes.get("href", "") for node in all_links} - set(firm_hrefs))
        try:
            raw = self._remote.execute_script("JSON.stringify([location.href, document.title])")
            url, title = json.loads(raw) if isinstance(raw, str) else ("", "")
        except (ValueError, ChromeException):
            url, title = "", ""
        logger.info(
            "Search page %d: %s, title %r; %d links to organizations (clicked), %d other result links (left alone), "
            "%d links to next pages",
            self._current_page,
            unquote(str(url)),
            title,
            len(firm_hrefs),
            other,
            pagination,
        )
        if self._snapshot_dir is not None:
            self._save_snapshot(firm_hrefs, [node.attributes.get("href", "") for node in all_links], pagination)

    def _save_snapshot(self, firm_hrefs: list[str], all_hrefs: list[str], pagination: int) -> None:
        """The page as it is (HTML) and the list of its result links, in a folder of a diagnostic run."""
        if self._snapshot_dir is None:
            return
        links = [
            {"position": number, "link": describe_link(href), "clicked": href in firm_hrefs}
            for number, href in enumerate(dict.fromkeys(all_hrefs), start=1)
        ]
        self._write_snapshot(
            "search",
            {"page": self._current_page, "next_pages": pagination, "links": links},
            suffix=f"_p{self._current_page}",
        )

    def _page_has_search_links_or_empty(self) -> bool:
        try:
            doc = self._read_document()
        except ChromeException:
            return False
        if doc.search(is_search_result_link):
            return True
        _, title, _ = self._remote.read_current_page()
        return bool(_NO_RESULTS_TITLE_RE.search(title))

    def _find_search_links(self, document: DOMNode) -> list[DOMNode]:
        return document.search(is_search_result_link)

    def _all_search_hrefs(self) -> set[str]:
        document = self._read_document()
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
        document = self._read_document()
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
        self._document_seconds = []
        self._lookup_seconds = []
        self._wait_for_stable_links()
        document = self._read_document()
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

        self._report_page(_firm_links_only(document), all_nodes, len(pages_ahead))

        collected = self._collect_items_from_links(
            _firm_links_only,
            status=200,
            block_checker=block_checker,
            max_items=max_items,
            refetch_on_stale=True,
        )
        items, item_urls, blocked_reason, stopped = (
            collected.items,
            collected.urls,
            collected.blocked_reason,
            collected.stopped,
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
        self._log_reading_time()
        return ScrapedSearchResult(
            snapshot=snapshot,
            items=items,
            has_more=False if blocked_reason is not None or stopped else has_more,
            blocked_reason=blocked_reason,
            stopped=stopped,
            links_found=len(all_hrefs),
            nothing_found=not all_hrefs and bool(_NO_RESULTS_TITLE_RE.search(snapshot.title)),
            unreadable=collected.unreadable,
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
            batch_result = self._collect_items_from_links(
                lambda doc: doc.search(is_in_building_firm_link),
                status=status,
                block_checker=block_checker,
                skip_hrefs=seen_hrefs,
                max_items=remaining,
                refetch_on_stale=True,
            )
            batch, batch_urls, attempted_hrefs = batch_result.items, batch_result.urls, batch_result.attempted
            batch_block, stopped = batch_result.blocked_reason, batch_result.stopped
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

            doc_after = self._read_document()
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
        snapshot_dir: Path | None = None,
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
        self._snapshot_dir = snapshot_dir
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
            snapshot_dir=self._snapshot_dir,
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
