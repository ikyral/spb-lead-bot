"""Unit and integration tests for the Chrome CDP browser layer and 2GIS click/pagination parsers."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.services.api_client import (
    MAX_RESPONSE_BYTES,
    ProviderBlockedError,
    ProviderNoConnectionError,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.providers.base import SearchArea, SearchQuery
from src.services.providers.twogis_scraper import ScraperOptions, TwoGisScraperProvider
from src.services.scraper.chrome.browser import ChromeBrowser
from src.services.scraper.chrome.dom import DOMNode
from src.services.scraper.chrome.exceptions import (
    ChromeException,
    ChromePathNotFound,
)
from src.services.scraper.chrome.filters import ITEM_RESPONSE_PATTERN, XHR_COUNTER_SCRIPT, blocked_requests
from src.services.scraper.chrome.options import ChromeOptions, ChromeParserOptions, default_memory_limit
from src.services.scraper.chrome.parsers import (
    ChromeScraperSession,
    InBuildingPageReader,
    SearchPageReader,
    classify_2gis_url,
    is_in_building_firm_link,
    is_search_result_link,
    strip_page_number,
)
from src.services.scraper.chrome.remote import ChromeRemote
from src.services.scraper.chrome.utils import floor_to_hundreds, free_port, locate_chrome_path, wait_until_finished
from src.services.scraper.robots import RobotsRules

QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _dom_link(node_id: int, href: str) -> DOMNode:
    return DOMNode(
        nodeId=node_id,
        backendNodeId=node_id * 10,
        nodeType=1,
        nodeName="A",
        localName="a",
        nodeValue="",
        attributes=["href", href],
        children=[],
    )


def _dom_root(*children: DOMNode) -> DOMNode:
    return DOMNode(
        nodeId=1,
        backendNodeId=10,
        nodeType=9,
        nodeName="#document",
        localName="",
        nodeValue="",
        children=list(children),
    )


def _byid_payload(firm_id: str, name: str, *, contacts: list[Any] | None = None) -> dict[str, Any]:
    return {
        "meta": {"code": 200},
        "result": {
            "items": [
                {
                    "id": firm_id,
                    "name": name,
                    "type": "branch",
                    "point": {"lat": 59.93, "lon": 30.31},
                    "address_name": "Невский проспект, 1",
                    "contact_groups": [] if contacts is None else [{"contacts": contacts}],
                }
            ]
        },
    }


def _allow_robots() -> Any:
    async def _load() -> RobotsRules:
        return RobotsRules()

    return _load


def _instant_sleep() -> Any:
    async def _sleep(_delay: float) -> None:
        return None

    return _sleep


# ---------------------------------------------------------------------------
# Utils, options, DOMNode, filters
# ---------------------------------------------------------------------------


def test_utils_and_memory_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert floor_to_hundreds(0) == 0
    assert floor_to_hundreds(199) == 100
    assert floor_to_hundreds(1650) == 1600
    assert free_port() > 0

    calls = 0

    @wait_until_finished(timeout=1, poll_interval=0.001, throw_exception=False)
    def _eventually() -> str:
        nonlocal calls
        calls += 1
        return "ok" if calls >= 3 else ""

    assert _eventually() == "ok"
    assert calls == 3

    @wait_until_finished(timeout=0.01, poll_interval=0.005, throw_exception=True)
    def _never() -> None:
        return None

    with pytest.raises(TimeoutError):
        _never()

    # Memory limit fallback via os.sysconf when psutil is unavailable.
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.setattr(os, "sysconf", lambda key: 4096 if key == "SC_PAGE_SIZE" else 512 * 1024, raising=False)
    # 2048 MB total * 0.75 = 1536 -> rounded down to 1500
    assert default_memory_limit() == 1500

    # locate_chrome_path checks playwright cache fallback when system binaries absent
    pw_chrome = tmp_path / ".cache" / "ms-playwright" / "chromium-1248" / "chrome-linux64" / "chrome"
    pw_chrome.parent.mkdir(parents=True)
    pw_chrome.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("os.path.isfile", lambda path: str(path) == str(pw_chrome))
    assert locate_chrome_path() == str(pw_chrome)


def test_dom_node_and_url_helpers() -> None:
    root = DOMNode.model_validate(
        {
            "nodeId": 1,
            "backendNodeId": 2,
            "nodeType": 1,
            "nodeName": "DIV",
            "localName": "div",
            "nodeValue": "",
            "attributes": ["class", "card"],
            "children": [
                {
                    "nodeId": 3,
                    "backendNodeId": 4,
                    "nodeType": 1,
                    "nodeName": "A",
                    "localName": "a",
                    "nodeValue": "",
                    "attributes": ["href", f"/spb/firm/100?stat={_b64('payload')}"],
                    "children": [],
                }
            ],
        }
    )
    assert root.attributes == {"class": "card"}
    matches = root.search(is_search_result_link)
    assert len(matches) == 1

    node_none = DOMNode(
        nodeId=1,
        backendNodeId=2,
        nodeType=1,
        nodeName="DIV",
        localName="div",
        nodeValue="",
        attributes=None,  # type: ignore[arg-type]
    )
    assert node_none.attributes == {}

    node_dict_none = DOMNode.model_validate(
        {
            "nodeId": 1,
            "backendNodeId": 2,
            "nodeType": 1,
            "nodeName": "DIV",
            "localName": "div",
            "nodeValue": "",
            "attributes": None,
        }
    )
    assert node_dict_none.attributes == {}

    node_omitted = DOMNode.model_validate(
        {"nodeId": 1, "backendNodeId": 2, "nodeType": 1, "nodeName": "DIV", "localName": "div", "nodeValue": ""}
    )
    assert node_omitted.attributes == {}

    with pytest.raises(ValueError, match="even length"):
        DOMNode(
            nodeId=1,
            backendNodeId=2,
            nodeType=1,
            nodeName="DIV",
            localName="div",
            nodeValue="",
            attributes=["dangling"],  # type: ignore[arg-type]
        )

    assert strip_page_number("https://2gis.ru/spb/search/cafe/page/6?m=1#hash") == "https://2gis.ru/spb/search/cafe?m=1#hash"
    assert classify_2gis_url("https://2gis.ru/spb/inside/700300761") == "in_building"
    assert classify_2gis_url("https://2gis.ru/spb/firm/700000010") == "firm"
    assert classify_2gis_url("https://2gis.ru/spb/search/%D0%BA%D0%B0%D1%84%D0%B5") == "search"

    bad_stat_link = _dom_link(2, "/spb/firm/100?stat=%%%not-b64%%%")
    station_link = _dom_link(3, f"/spb/station/200?stat={_b64('st')}")
    encoded_stat_link = _dom_link(4, f"/spb/firm/101?stat={_b64('payload').replace('=', '%3D')}")
    unpadded_stat_link = _dom_link(5, f"/spb/firm/102?stat={_b64('payload').rstrip('=')}")
    urlsafe_stat_link = _dom_link(6, "/spb/firm/103?stat=-__-")
    whitespace_stat_link = _dom_link(7, "/spb/firm/104?stat=%20")
    empty_stat_link = _dom_link(8, "/spb/firm/105?stat=")
    assert not is_search_result_link(bad_stat_link)
    assert is_search_result_link(station_link)
    assert is_search_result_link(encoded_stat_link)
    assert is_search_result_link(unpadded_stat_link)
    assert is_search_result_link(urlsafe_stat_link)
    assert not is_search_result_link(whitespace_stat_link)
    assert not is_search_result_link(empty_stat_link)

    assert is_in_building_firm_link(_dom_link(4, "/spb/firm/999?m=1"))
    assert is_in_building_firm_link(_dom_link(6, "/spb/firm/999?utm_source=2gis/web"))
    assert is_in_building_firm_link(_dom_link(7, "https://2gis.ru/spb/firm/999"))
    assert is_in_building_firm_link(_dom_link(8, "/spb/firm/999/"))
    assert is_in_building_firm_link(_dom_link(9, "https://2gis.ru/spb/firm/999/?utm_source=2gis/web"))
    assert not is_in_building_firm_link(_dom_link(5, "/spb/firm/999/reviews"))

    core = blocked_requests(extended=False)
    ext = blocked_requests(extended=True)
    assert len(ext) > len(core)
    assert any("ads" in pattern for pattern in core)
    assert any("tile*" in pattern for pattern in ext)
    assert "window.openHTTPs = 0;" in XHR_COUNTER_SCRIPT


# ---------------------------------------------------------------------------
# ChromeBrowser & ChromeRemote
# ---------------------------------------------------------------------------


def test_chrome_browser_flags_and_profile_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.services.scraper.chrome.browser.locate_chrome_path", lambda: None)
    with pytest.raises(ChromePathNotFound):
        ChromeBrowser(ChromeOptions())

    fake_bin = tmp_path / "chrome"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    spawned_cmds: list[list[str]] = []
    proc_mock = MagicMock()
    proc_mock.poll.side_effect = [None, 0]
    proc_mock.wait.side_effect = [subprocess.TimeoutExpired(cmd="chrome", timeout=5), 0]

    def _fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        spawned_cmds.append(cmd)
        return proc_mock

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    # Persistent profile must NOT be deleted on close
    persist_dir = tmp_path / "persist_profile"
    browser = ChromeBrowser(
        ChromeOptions(
            binary_path=fake_bin,
            profile_dir=persist_dir,
            headless=True,
            disable_images=True,
            memory_limit=1500,
        )
    )
    assert "--headless=new" in spawned_cmds[0]
    assert "--window-size=1920,1080" in spawned_cmds[0]
    assert "--blink-settings=imagesEnabled=false" in spawned_cmds[0]
    assert "--js-flags=--expose-gc --max-old-space-size=1500" in spawned_cmds[0]
    assert f"--user-data-dir={persist_dir}" in spawned_cmds[0]
    assert persist_dir.is_dir()

    browser.close()
    proc_mock.terminate.assert_called_once()
    proc_mock.kill.assert_called_once()
    assert persist_dir.is_dir()

    # Temporary auto-created profile MUST be removed on close
    proc2 = MagicMock()
    proc2.poll.return_value = 0
    monkeypatch.setattr(subprocess, "Popen", lambda _cmd, **_kw: proc2)
    temp_browser = ChromeBrowser(ChromeOptions(binary_path=fake_bin, profile_dir=None))
    temp_dir = Path(temp_browser.profile_path)
    assert temp_dir.is_dir()
    temp_browser.close()
    assert not temp_dir.exists()


def test_chrome_remote_setup_and_network_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_bin = tmp_path / "chrome"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    class _FakeBrowserProcess:
        def __init__(self, _opts: ChromeOptions) -> None:
            self.remote_port = 9222
            self.returncode = None

        def close(self) -> None:
            return None

    class _FakeTab:
        status_started = "started"

        def __init__(self) -> None:
            self.id = "tab-1"
            self.status = "started"
            self._stopped = MagicMock()
            self._stopped.is_set.return_value = False
            self._stopped.wait.return_value = True
            self.Network = MagicMock()
            self.Emulation = MagicMock()
            self.DOM = MagicMock()
            self.Page = MagicMock()
            self.Runtime = MagicMock()
            self.Log = MagicMock()
            self.Runtime.evaluate.return_value = {"result": {"value": "Mozilla/5.0 HeadlessChrome/125.0"}}
            self._send = MagicMock(return_value={})

        def start(self) -> None:
            return None

        def stop(self) -> None:
            self._stopped.is_set.return_value = True

        def call_method(self, method: str, **_kwargs: Any) -> dict[str, Any]:
            if method == "Network.getResponseBody":
                return {"body": base64.b64encode(b'{"ok":true}').decode("ascii"), "base64Encoded": True}
            return {}

    fake_tab = _FakeTab()

    monkeypatch.setattr("src.services.scraper.chrome.remote.ChromeBrowser", _FakeBrowserProcess)
    monkeypatch.setattr("src.services.scraper.chrome.remote.patch_pychrome", lambda: None)

    import sys as _sys
    import types

    fake_pychrome = types.SimpleNamespace(
        Browser=lambda url: MagicMock(),
        Tab=_FakeTab,
        UserAbortException=type("UserAbortException", (Exception,), {}),
        RuntimeException=type("RuntimeException", (Exception,), {}),
        CallMethodException=type("CallMethodException", (Exception,), {}),
    )
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {}
    import requests as _requests

    monkeypatch.setattr(_requests, "put", lambda *_a, **_kw: fake_resp)
    monkeypatch.setattr(_requests, "get", lambda *_a, **_kw: MagicMock(json=lambda: [{"id": "tab-1"}]))
    monkeypatch.setitem(_sys.modules, "pychrome", fake_pychrome)
    monkeypatch.setattr(ChromeRemote, "_create_tab", lambda self: fake_tab)

    remote = ChromeRemote(ChromeOptions(binary_path=fake_bin, headless=True), [ITEM_RESPONSE_PATTERN])
    remote.start()
    try:
        # Headless stripped from User-Agent + viewport set + webdriver masked
        fake_tab.Network.setUserAgentOverride.assert_called_once()
        ua_arg = fake_tab.Network.setUserAgentOverride.call_args.kwargs["userAgent"]
        assert "Headless" not in ua_arg
        fake_tab.Emulation.setDeviceMetricsOverride.assert_called_once_with(
            width=1920,
            height=1080,
            deviceScaleFactor=1,
            mobile=False,
        )
        fake_tab.Page.addScriptToEvaluateOnNewDocument.assert_called_once()
        assert "webdriver" in fake_tab.Page.addScriptToEvaluateOnNewDocument.call_args.kwargs["source"]

        # Request/response + loadingFailed metadata + get_document_response picks Document
        remote._chrome_tab.Network.requestWillBeSent(
            requestId="req-1",
            type="Document",
            request={"url": "https://2gis.ru/spb/search/x"},
        )
        remote._chrome_tab.Network.loadingFailed(
            requestId="req-1",
            type="Document",
            errorText="net::ERR_NAME_NOT_RESOLVED",
        )
        doc = remote.get_document_response()
        assert doc is not None
        assert doc["status"] == -1
        assert doc["meta"]["type"] == "Document"

        # Pattern queue + base64 get_response_body
        remote._chrome_tab.Network.requestWillBeSent(
            requestId="req-2",
            type="XHR",
            request={"url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1"},
        )
        remote._chrome_tab.Network.responseReceived(
            requestId="req-2",
            type="XHR",
            response={"url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1", "status": 200},
        )
        intercepted = remote.wait_response(ITEM_RESPONSE_PATTERN)
        assert intercepted is not None
        assert remote.get_response_body(intercepted) == '{"ok":true}'

        # Oversize state guard
        fake_tab.Runtime.evaluate.return_value = {
            "result": {"value": json.dumps([{"json": "x" * (MAX_RESPONSE_BYTES + 1)}, {"problem": "missing"}])}
        }
        values, problems = remote.read_page_state(("initialState", "otherVar"))
        assert values == {}
        assert problems == {"initialState": "значение слишком большое", "otherVar": "missing"}
    finally:
        remote.stop()


# ---------------------------------------------------------------------------
# SearchPageReader & InBuildingPageReader & ChromeScraperSession
# ---------------------------------------------------------------------------


class _FakeRemote:
    """In-memory programmable ChromeRemote double for SearchPageReader & InBuildingPageReader tests."""

    def __init__(self) -> None:
        self.navigations: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.scripts: list[str] = []
        self.cleared_count = 0
        self.doc_status: int | None = 200
        self.current_page_info: tuple[str, str, str] = (
            "https://2gis.ru/spb/search/x",
            "Стоматологии — 2ГИС",
            "<html></html>",
        )
        self.page_state: tuple[dict[str, Any], dict[str, str]] = ({}, {})
        self.open_https_values: list[int | None] = [0]
        self.documents: list[DOMNode] = []
        self.click_outcomes: list[Any] = []
        self.responses_by_url: list[dict[str, Any]] = []
        self._stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._stopped = True

    def add_start_script(self, _source: str) -> None:
        return None

    def add_blocked_requests(self, _urls: list[str]) -> bool:
        return True

    def clear_requests(self) -> None:
        self.cleared_count += 1

    def drain_responses(self, _pattern: str) -> None:
        return None

    def navigate(self, url: str, referer: str = "", timeout: int = 60) -> None:
        self.navigations.append((url, referer))

    def get_document_response(self) -> dict[str, Any] | None:
        if self.doc_status is None:
            return None
        return {"status": self.doc_status, "meta": {"type": "Document"}}

    def execute_script(self, expression: str) -> Any:
        self.scripts.append(expression)
        if "readyState" in expression:
            open_val = self.open_https_values.pop(0) if len(self.open_https_values) > 1 else self.open_https_values[0]
            return json.dumps({"ready": "complete", "open": open_val})
        if "window.openHTTPs" in expression:
            return None
        return None

    def get_document(self, full: bool = True) -> DOMNode:
        if len(self.documents) > 1:
            return self.documents.pop(0)
        return self.documents[0]

    def perform_click(self, dom_node: DOMNode) -> None:
        href = dom_node.attributes.get("href", "")
        self.clicks.append(href)
        if "/page/" in href:
            page_num = href.split("/page/")[1]
            self.current_page_info = (
                f"https://2gis.ru/spb/search/x/page/{page_num}",
                "Стоматологии — 2ГИС",
                "<html></html>",
            )

    def wait_response(self, _pattern: str) -> dict[str, Any] | None:
        if not self.click_outcomes:
            return None
        outcome = self.click_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_response_body(self, response: dict[str, Any]) -> str:
        return str(response.get("body") or "")

    def get_responses(self) -> list[dict[str, Any]]:
        return list(self.responses_by_url)

    def read_current_page(self) -> tuple[str, str, str]:
        return self.current_page_info

    def read_page_state(self) -> tuple[dict[str, Any], dict[str, str]]:
        return self.page_state

    def bring_to_front(self) -> None:
        return None


def test_search_reader_clicks_retries_pagination_walking_and_gc() -> None:
    remote = _FakeRemote()
    p1_doc = _dom_root(
        _dom_link(2, f"/spb/firm/101?stat={_b64('a')}"),
        _dom_link(3, f"/spb/station/901?stat={_b64('s')}"),  # skipped for click, tracked in visited
        _dom_link(4, "/spb/search/x/page/2"),
    )
    p2_doc = _dom_root(
        _dom_link(5, f"/spb/firm/202?stat={_b64('b')}"),
        _dom_link(6, "/spb/search/x/page/6"),
    )
    p6_doc = _dom_root(
        _dom_link(7, f"/spb/firm/606?stat={_b64('c')}"),
        _dom_link(8, "/spb/search/x/page/7"),
    )
    # Feed enough DOM reads for stale check + scrape per page
    remote.documents = [
        p1_doc, p1_doc, p1_doc, p1_doc, p1_doc,  # page 1
        p1_doc, p2_doc, p2_doc, p2_doc,  # page 1->2 advance + scrape
        p2_doc, p6_doc, p6_doc, p6_doc,  # page 2->6 advance + scrape
    ]
    # First click attempt on firm/101 returns None (retry succeeds!), firm/202 succeeds, firm/606 succeeds
    remote.click_outcomes = [
        None,
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=101",
            "body": json.dumps(_byid_payload("101", "Клиника 1")),
        },
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=202",
            "body": json.dumps(_byid_payload("202", "Клиника 2")),
        },
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=606",
            "body": json.dumps(_byid_payload("606", "Клиника 6")),
        },
    ]

    reader = SearchPageReader(
        remote,  # type: ignore[arg-type]
        ChromeParserOptions(use_gc=True, gc_pages_interval=2),
    )

    r1 = reader.read_page("https://2gis.ru/spb/search/x/page/3", page=1)
    assert remote.navigations == [("https://2gis.ru/spb/search/x", "https://google.com")]
    assert [p["result"]["items"][0]["id"] for p in r1.items] == ["101"]
    assert r1.has_more is True
    assert not any("/station/" in href for href in remote.clicks)

    # Sequential page 2 walks via DOM pagination link (NO extra navigate!)
    r2 = reader.read_page("https://2gis.ru/spb/search/x", page=2)
    assert len(remote.navigations) == 1
    assert [p["result"]["items"][0]["id"] for p in r2.items] == ["202"]
    assert any("window.gc" in expr for expr in remote.scripts)  # page 2 % interval 2 == 0

    # Walk next page (page 6 label) without redirect to page 1
    r6 = reader.read_page("https://2gis.ru/spb/search/x", page=6)
    assert len(remote.navigations) == 1
    assert [p["result"]["items"][0]["id"] for p in r6.items] == ["606"]
    assert r6.has_more is True
    assert remote.cleared_count >= 3


def test_search_reader_404_and_partial_block_preservation() -> None:
    remote = _FakeRemote()
    remote.doc_status = 404
    reader = SearchPageReader(remote, ChromeParserOptions(skip_404_response=True))  # type: ignore[arg-type]
    r404 = reader.read_page("https://2gis.ru/spb/search/missing", page=1)
    assert r404.not_found is True
    assert r404.items == []
    assert r404.has_more is False

    # Partial block mid-page: firm/1 succeeds, firm/2 triggers HTTP 403
    remote2 = _FakeRemote()
    doc = _dom_root(
        _dom_link(2, f"/spb/firm/1?stat={_b64('1')}"),
        _dom_link(3, f"/spb/firm/2?stat={_b64('2')}"),
        _dom_link(4, "/spb/search/x/page/2"),
    )
    remote2.documents = [doc]
    remote2.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1",
            "body": json.dumps(_byid_payload("1", "Первая")),
        },
        {"status": 403, "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=2", "body": ""},
    ]
    reader2 = SearchPageReader(remote2, ChromeParserOptions())  # type: ignore[arg-type]
    res = reader2.read_page("https://2gis.ru/spb/search/x", page=1)
    assert res.blocked_reason is not None
    assert "403" in res.blocked_reason
    assert len(res.items) == 1
    assert res.items[0]["result"]["items"][0]["id"] == "1"
    assert res.has_more is False


def test_search_reader_duplicate_same_page_links_avoid_refetch() -> None:
    """Ensure duplicate cards on same page pair DOMNode correctly without index drift or extra refetch."""
    remote = _FakeRemote()
    # firm 101 appears twice (e.g. image + title link), followed by firm 102
    doc = _dom_root(
        _dom_link(2, f"/spb/firm/101?stat={_b64('101')}"),
        _dom_link(3, f"/spb/firm/101?stat={_b64('101')}"),
        _dom_link(4, f"/spb/firm/102?stat={_b64('102')}"),
    )
    remote.documents = [doc]
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=101",
            "body": json.dumps(_byid_payload("101", "Первая")),
        },
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=102",
            "body": json.dumps(_byid_payload("102", "Вторая")),
        },
    ]
    reader = SearchPageReader(remote, ChromeParserOptions())  # type: ignore[arg-type]
    res = reader.read_page("https://2gis.ru/spb/search/x", page=1)
    assert len(res.items) == 2
    assert [p["result"]["items"][0]["id"] for p in res.items] == ["101", "102"]
    assert len(remote.clicks) == 2


def test_in_building_reader_lazy_loading() -> None:
    remote = _FakeRemote()
    remote.current_page_info = ("https://2gis.ru/spb/inside/555", "В здании — 2ГИС", "<html></html>")
    doc_batch1 = _dom_root(_dom_link(2, "/spb/firm/10"))
    doc_batch2 = _dom_root(_dom_link(2, "/spb/firm/10"), _dom_link(3, "/spb/firm/20"))
    remote.documents = [
        doc_batch1,  # get_document inside first _collect_items_from_links
        doc_batch2,  # doc_after checks newly appeared links
        doc_batch2,  # second _collect_items_from_links (skips /firm/10, clicks /firm/20)
        doc_batch2,  # doc_after finds no new hrefs -> stops
    ]
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=10",
            "body": json.dumps(_byid_payload("10", "Фирма 10")),
        },
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=20",
            "body": json.dumps(_byid_payload("20", "Фирма 20")),
        },
    ]

    reader = InBuildingPageReader(remote, ChromeParserOptions())  # type: ignore[arg-type]
    res = reader.read_page("https://2gis.ru/spb/inside/555")
    assert remote.clicks == ["/spb/firm/10", "/spb/firm/20"]
    assert [p["result"]["items"][0]["id"] for p in res.items] == ["10", "20"]
    assert res.has_more is False


# ---------------------------------------------------------------------------
# ChromeScraperSession + TwoGisScraperProvider integration
# ---------------------------------------------------------------------------


async def test_chrome_session_error_translation_and_assisted_images_flag() -> None:
    def _no_chrome(_opts: ChromeOptions, _patterns: list[str]) -> Any:
        raise ChromePathNotFound()

    session = ChromeScraperSession(remote_factory=_no_chrome)
    with pytest.raises(ProviderUnavailableError):
        await session.start()

    # Assisted mode forces disable_images=False so human sees captcha images
    assisted_session = ChromeScraperSession(disable_images=True, assisted=True)
    assert assisted_session._chrome_options.disable_images is False
    headless_session = ChromeScraperSession(disable_images=True, assisted=False)
    assert headless_session._chrome_options.disable_images is True

    # Offline error does NOT count as a site visit on provider
    offline_remote = _FakeRemote()
    offline_remote.navigate = MagicMock(side_effect=ChromeException("net::ERR_INTERNET_DISCONNECTED"))  # type: ignore[method-assign]
    offline_session = ChromeScraperSession(remote_factory=lambda _o, _p: offline_remote)  # type: ignore[arg-type,return-value]
    provider = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: offline_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        with pytest.raises(ProviderNoConnectionError):
            await provider.search(QUERY, AREA, page=1)
        assert provider.run_counted is False
    finally:
        await provider.close()

    # Generic Chrome failure maps to ProviderTemporaryError and marks run_counted
    broken_remote = _FakeRemote()
    broken_remote.navigate = MagicMock(side_effect=ChromeException("CDP command timed out"))  # type: ignore[method-assign]
    broken_session = ChromeScraperSession(remote_factory=lambda _o, _p: broken_remote)  # type: ignore[arg-type,return-value]
    provider2 = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: broken_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        with pytest.raises(ProviderTemporaryError):
            await provider2.search(QUERY, AREA, page=1)
        assert provider2.run_counted is True
    finally:
        await provider2.close()


async def test_provider_chrome_integration_direct_urls_partial_block_and_assisted() -> None:
    remote = _FakeRemote()
    # Direct firm URL with HTTP 404 -> read_card returns None cleanly
    remote.doc_status = 404
    chrome_session = ChromeScraperSession(remote_factory=lambda _o, _p: remote)  # type: ignore[arg-type,return-value]
    provider = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: chrome_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        card = await provider.read_card("https://2gis.ru/spb/firm/404", "404")
        assert card is None
        assert provider.run_counted is True
    finally:
        await provider.close()

    # Assisted captcha recovery mid-page preserves item 1 and merges item 2 without duplicates
    remote_assisted = _FakeRemote()
    doc = _dom_root(
        _dom_link(2, f"/spb/firm/1?stat={_b64('1')}"),
        _dom_link(3, f"/spb/firm/2?stat={_b64('2')}"),
    )
    remote_assisted.documents = [doc]
    # Simulate item 1 collected -> captcha hits on item 2 -> human resolves -> retry yields item 1 + item 2
    captcha_state = {"solved": False}

    captcha_html = (Path(__file__).parent / "fixtures" / "twogis_captcha.html").read_text(encoding="utf-8")

    def _assisted_wait(_pattern: str) -> dict[str, Any] | None:
        if not captcha_state["solved"] and len(remote_assisted.clicks) == 2:
            remote_assisted.current_page_info = (
                "https://captcha.2gis.ru/form?return_url=https://2gis.ru/spb/search/x",
                "2GIS Captcha",
                captcha_html,
            )
            return {"status": 403, "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=2", "body": ""}
        if remote_assisted.clicks[-1].startswith("/spb/firm/1"):
            return {
                "status": 200,
                "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1",
                "body": json.dumps(_byid_payload("1", "Первая")),
            }
        return {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=2",
            "body": json.dumps(
                _byid_payload(
                    "2",
                    "Вторая",
                    contacts=[{"type": "website", "url": "https://second.example"}],
                )
            ),
        }

    remote_assisted.wait_response = _assisted_wait  # type: ignore[method-assign]

    async def _solve_during_wait(_delay: float) -> None:
        captcha_state["solved"] = True
        remote_assisted.current_page_info = (
            "https://2gis.ru/spb/search/стоматология",
            "Стоматологии — 2ГИС",
            "<html>ok</html>",
        )

    assisted_session = ChromeScraperSession(
        headless=False,
        assisted=True,
        remote_factory=lambda _o, _p: remote_assisted,  # type: ignore[arg-type,return-value]
    )
    provider_assisted = TwoGisScraperProvider(
        options=ScraperOptions(headless=False, min_delay_seconds=3.0),
        session_factory=lambda: assisted_session,
        robots_loader=_allow_robots(),
        sleep=_solve_during_wait,
    )
    try:
        with pytest.raises(ProviderBlockedError) as exc_info:
            await provider_assisted.search(QUERY, AREA, page=1)
        assert [place.name for place in exc_info.value.partial] == ["Первая"]
        # Item 1 has empty contact_groups=[] from /items/byid -> did NOT trigger extra card navigation
        assert len(remote_assisted.navigations) == 1
    finally:
        await provider_assisted.close()

    # Direct /inside/ URL preserved verbatim in _search_url
    direct_inside = SearchQuery(text="https://2gis.ru/spb/inside/700300761", category="mall", high_margin=False)
    assert provider_assisted._search_url(direct_inside, AREA, page=1) == "https://2gis.ru/spb/inside/700300761"

    # Settings wiring via ScraperOptions.from_settings
    cfg = Settings(
        _env_file=None,
        SCRAPER_CHROME_PATH="/usr/bin/google-chrome",
        SCRAPER_DISABLE_IMAGES=False,
        SCRAPER_CLICK_DELAY_MS=250,
        SCRAPER_USE_GC=True,
    )
    loaded_opts = ScraperOptions.from_settings(cfg)
    assert loaded_opts.chrome_path == Path("/usr/bin/google-chrome")
    assert loaded_opts.disable_images is False
    assert loaded_opts.click_delay_ms == 250
    assert loaded_opts.use_gc is True

    # Direct /firm/ URL searches a single card cleanly
    remote_firm = _FakeRemote()
    remote_firm.current_page_info = ("https://2gis.ru/spb/firm/555", "Прямая фирма — 2ГИС", "<html></html>")
    remote_firm.responses_by_url = [
        {
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=555",
            "mimeType": "application/json",
            "body": json.dumps(
                _byid_payload(
                    "555",
                    "Прямая фирма",
                    contacts=[{"type": "website", "url": "https://direct.example"}],
                )
            ),
        }
    ]
    firm_session = ChromeScraperSession(remote_factory=lambda _o, _p: remote_firm)  # type: ignore[arg-type,return-value]
    provider_firm = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: firm_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        direct_firm_res = await provider_firm.search(
            SearchQuery(text="https://2gis.ru/spb/firm/555", category="dentist_clinic", high_margin=True),
            AREA,
            page=1,
        )
        assert [p.name for p in direct_firm_res.places] == ["Прямая фирма"]
        assert direct_firm_res.has_more is False
    finally:
        await provider_firm.close()

    # Fallback to passive catalog JSON response when Chrome reader finds 0 clicked links
    remote_ssr = _FakeRemote()
    remote_ssr.documents = [_dom_root()]
    remote_ssr.current_page_info = ("https://2gis.ru/spb/search/ssr", "Стоматологии — 2ГИС", "<html></html>")
    remote_ssr.responses_by_url = [
        {
            "url": "https://catalog.api.2gis.ru/3.0/items?q=ssr",
            "mimeType": "application/json",
            "body": json.dumps(
                {
                    "result": {
                        "total": 1,
                        "items": [
                            {
                                "id": "77",
                                "name": "Passive API Клиника",
                                "type": "branch",
                                "point": {"lat": 59.93, "lon": 30.31},
                                "contact_groups": [{"contacts": [{"type": "website", "url": "https://ssr.example"}]}],
                            }
                        ],
                    }
                }
            ),
        }
    ]
    ssr_session = ChromeScraperSession(remote_factory=lambda _o, _p: remote_ssr)  # type: ignore[arg-type,return-value]
    provider_ssr = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: ssr_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        ssr_res = await provider_ssr.search(QUERY, AREA, page=1)
        assert [place.name for place in ssr_res.places] == ["Passive API Клиника"]
    finally:
        await provider_ssr.close()

    # Non-assisted HTTP 403 block raises ProviderBlockedError with partial places intact
    remote_block = _FakeRemote()
    doc_block = _dom_root(_dom_link(2, f"/spb/firm/1?stat={_b64('1')}"), _dom_link(3, f"/spb/firm/2?stat={_b64('2')}"))
    remote_block.documents = [doc_block]
    remote_block.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1",
            "body": json.dumps(_byid_payload("1", "Первая")),
        },
        {"status": 403, "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=2", "body": ""},
    ]
    blocked_session = ChromeScraperSession(remote_factory=lambda _o, _p: remote_block)  # type: ignore[arg-type,return-value]
    provider_blocked = TwoGisScraperProvider(
        options=ScraperOptions(min_delay_seconds=3.0),
        session_factory=lambda: blocked_session,
        robots_loader=_allow_robots(),
        sleep=_instant_sleep(),
    )
    try:
        with pytest.raises(ProviderBlockedError) as exc_info:
            await provider_blocked.search(QUERY, AREA, page=1)
        assert [p.name for p in exc_info.value.partial] == ["Первая"]
    finally:
        await provider_blocked.close()
