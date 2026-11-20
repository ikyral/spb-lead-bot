"""Unit tests for the Chrome CDP browser layer and 2GIS click/pagination parsers."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.services.api_client import (
    MAX_RESPONSE_BYTES,
    ProviderNoConnectionError,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.scraper.chrome import parsers as parsers_module
from src.services.scraper.chrome import utils as chrome_utils
from src.services.scraper.chrome.browser import ChromeBrowser
from src.services.scraper.chrome.dom import DOMNode
from src.services.scraper.chrome.exceptions import (
    ChromeException,
    ChromePathNotFound,
)
from src.services.scraper.chrome.filters import ITEM_RESPONSE_PATTERN
from src.services.scraper.chrome.options import ChromeOptions, ChromeParserOptions, default_memory_limit
from src.services.scraper.chrome.parsers import (
    ChromeScraperSession,
    GateAction,
    GateDecision,
    InBuildingPageReader,
    SearchPageReader,
    classify_2gis_url,
    is_in_building_firm_link,
    is_search_result_href,
    is_search_result_link,
    strip_page_number,
)
from src.services.scraper.chrome.remote import ChromeRemote
from src.services.scraper.chrome.utils import floor_to_hundreds, free_port, locate_chrome_path, wait_until_finished


@pytest.fixture(autouse=True)
def _no_wait_for_the_list_to_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reads of the result list are not spaced out by a real second here."""
    monkeypatch.setattr(parsers_module, "_LINKS_POLL_SECONDS", 0.0)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _go(_action: GateAction) -> GateDecision:
    return GateDecision.GO


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


# ---------------------------------------------------------------------------
# Utils, options, DOMNode
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
    with monkeypatch.context() as patched:
        patched.setattr(os, "sysconf", lambda key: 4096 if key == "SC_PAGE_SIZE" else 512 * 1024, raising=False)
        # 2048 MB total * 0.75 = 1536 -> rounded down to 1500
        assert default_memory_limit() == 1500
    # Windows has no os.sysconf: a fixed limit, no failure.
    with monkeypatch.context() as patched:
        patched.delattr(os, "sysconf", raising=False)
        assert default_memory_limit() == 2000

    # locate_chrome_path checks playwright cache fallback when system binaries absent
    pw_chrome = tmp_path / ".cache" / "ms-playwright" / "chromium-1248" / "chrome-linux64" / "chrome"
    pw_chrome.parent.mkdir(parents=True)
    pw_chrome.write_text("", encoding="utf-8")
    monkeypatch.setattr(chrome_utils, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
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

    assert (
        strip_page_number("https://2gis.ru/spb/search/cafe/page/6?m=1#hash")
        == "https://2gis.ru/spb/search/cafe?m=1#hash"
    )
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
    browser = ChromeBrowser(ChromeOptions(binary_path=fake_bin, profile_dir=persist_dir, memory_limit=1500))
    cmd = spawned_cmds[0]
    port = browser.remote_port
    assert f"--remote-debugging-port={port}" in cmd
    assert f"--remote-allow-origins=http://127.0.0.1:{port}" in cmd
    assert "--remote-allow-origins=*" not in cmd
    assert "--start-maximized" in cmd
    assert "--no-first-run" in cmd
    assert "--no-default-browser-check" in cmd
    assert "--js-flags=--expose-gc --max-old-space-size=1500" in cmd
    assert f"--user-data-dir={persist_dir}" in cmd
    # The window is always visible and the browser keeps its own defaults.
    for removed in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-fre", "--window-size"):
        assert not any(arg.startswith(removed) for arg in cmd)
    assert not any("blink-settings" in arg or "headless" in arg.lower() for arg in cmd)
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


def _fake_cdp_modules(monkeypatch: pytest.MonkeyPatch, tab_class: type) -> None:
    """pychrome, requests and websocket are not installed on the server: fakes stand in for them."""
    fake_pychrome = types.SimpleNamespace(
        Browser=lambda url: MagicMock(),
        Tab=tab_class,
        UserAbortException=type("UserAbortException", (Exception,), {}),
        RuntimeException=type("RuntimeException", (Exception,), {}),
        CallMethodException=type("CallMethodException", (Exception,), {}),
    )
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {}
    fake_requests = types.ModuleType("requests")
    fake_requests.put = lambda *_a, **_kw: fake_resp  # type: ignore[attr-defined]
    fake_requests.get = lambda *_a, **_kw: MagicMock(json=lambda: [{"id": "tab-1"}])  # type: ignore[attr-defined]
    fake_exceptions = types.ModuleType("requests.exceptions")
    fake_exceptions.RequestException = type("RequestException", (Exception,), {})  # type: ignore[attr-defined]
    fake_requests.exceptions = fake_exceptions  # type: ignore[attr-defined]
    fake_websocket = types.ModuleType("websocket")
    fake_websocket.WebSocketException = type("WebSocketException", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pychrome", fake_pychrome)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "requests.exceptions", fake_exceptions)
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)


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
            self.Runtime.evaluate.return_value = {"result": {"value": None}}
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
    _fake_cdp_modules(monkeypatch, _FakeTab)
    monkeypatch.setattr(ChromeRemote, "_create_tab", lambda self: fake_tab)

    remote = ChromeRemote(ChromeOptions(binary_path=fake_bin), [ITEM_RESPONSE_PATTERN])
    remote.start()
    try:
        # Nothing about the browser or its pages is changed: no overrides, no scripts injected.
        fake_tab.Network.setUserAgentOverride.assert_not_called()
        fake_tab.Network.setBlockedURLs.assert_not_called()
        assert fake_tab.Emulation.method_calls == []
        fake_tab.Page.addScriptToEvaluateOnNewDocument.assert_not_called()
        fake_tab.Runtime.evaluate.assert_not_called()
        fake_tab.Network.enable.assert_called_once()

        # Navigation goes without a referrer.
        remote.navigate("https://2gis.ru/spb/search/x", timeout=30)
        fake_tab.Page.navigate.assert_called_once_with(url="https://2gis.ru/spb/search/x", _timeout=30)

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
        self.navigations: list[str] = []
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
        # Either a document per search page (switched by pagination clicks) or a queue of documents.
        self.pages: dict[int, DOMNode] = {}
        self.current_page = 1
        self.documents: list[DOMNode] = []
        self.click_outcomes: list[Any] = []
        self.responses_by_url: list[dict[str, Any]] = []
        self._stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._stopped = True

    def clear_requests(self) -> None:
        self.cleared_count += 1

    def drain_responses(self, _pattern: str) -> None:
        return None

    def navigate(self, url: str, timeout: int = 60) -> None:
        self.navigations.append(url)
        self.current_page = 1

    def get_document_response(self) -> dict[str, Any] | None:
        if self.doc_status is None:
            return None
        return {"status": self.doc_status, "meta": {"type": "Document"}}

    def execute_script(self, expression: str) -> Any:
        self.scripts.append(expression)
        if expression == "document.readyState":
            return "complete"
        return None

    def get_document(self, full: bool = True) -> DOMNode:
        if self.pages:
            return self.pages[self.current_page]
        if len(self.documents) > 1:
            return self.documents.pop(0)
        return self.documents[0]

    def perform_click(self, dom_node: DOMNode) -> None:
        href = dom_node.attributes.get("href", "")
        self.clicks.append(href)
        if "/page/" in href:
            page_num = href.split("/page/")[1]
            self.current_page = int(page_num)
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
    remote.pages = {
        1: _dom_root(
            _dom_link(2, f"/spb/firm/101?stat={_b64('a')}"),
            _dom_link(3, f"/spb/station/901?stat={_b64('s')}"),  # skipped for click, tracked in visited
            _dom_link(4, "/spb/search/x/page/2"),
        ),
        2: _dom_root(
            _dom_link(5, f"/spb/firm/202?stat={_b64('b')}"),
            _dom_link(6, "/spb/search/x/page/6"),
        ),
        6: _dom_root(
            _dom_link(7, f"/spb/firm/606?stat={_b64('c')}"),
            _dom_link(8, "/spb/search/x/page/7"),
        ),
    }
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

    actions: list[GateAction] = []

    def _recording_gate(action: GateAction) -> GateDecision:
        actions.append(action)
        return GateDecision.GO

    reader = SearchPageReader(
        remote,  # type: ignore[arg-type]
        ChromeParserOptions(use_gc=True, gc_pages_interval=2),
        gate=_recording_gate,
    )

    r1 = reader.read_page("https://2gis.ru/spb/search/x/page/3", page=1)
    assert remote.navigations == ["https://2gis.ru/spb/search/x"]
    assert [p["result"]["items"][0]["id"] for p in r1.items] == ["101"]
    assert r1.has_more is True
    assert r1.links_found == 2
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

    # The gate was asked before every navigation and every click, the retried click included.
    assert [a.kind for a in actions] == ["open", "card", "card", "page", "card", "page", "card"]
    assert len(actions) == len(remote.navigations) + len(remote.clicks)
    assert [a.firm_id for a in actions if a.kind == "card"] == ["101", "101", "202", "606"]
    assert [a.page for a in actions if a.kind == "page"] == [2, 6]


def test_search_reader_gate_skip_and_stop() -> None:
    remote = _FakeRemote()
    remote.pages = {
        1: _dom_root(
            _dom_link(2, f"/spb/firm/1?stat={_b64('1')}"),
            _dom_link(3, f"/spb/firm/2?stat={_b64('2')}"),
            _dom_link(4, f"/spb/firm/3?stat={_b64('3')}"),
            _dom_link(5, "/spb/search/x/page/2"),
        ),
    }
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=1",
            "body": json.dumps(_byid_payload("1", "Первая")),
        },
    ]

    def _gate(action: GateAction) -> GateDecision:
        if action.firm_id == "2":
            return GateDecision.SKIP
        if action.firm_id == "3":
            return GateDecision.STOP
        return GateDecision.GO

    reader = SearchPageReader(remote, ChromeParserOptions(), gate=_gate)  # type: ignore[arg-type]
    res = reader.read_page("https://2gis.ru/spb/search/x", page=1)
    assert remote.clicks == [f"/spb/firm/1?stat={_b64('1')}"]
    assert res.stopped is True
    assert res.has_more is False
    assert [p["result"]["items"][0]["id"] for p in res.items] == ["1"]

    # A refused first navigation opens nothing at all.
    closed = _FakeRemote()
    closed_reader = SearchPageReader(
        closed,  # type: ignore[arg-type]
        ChromeParserOptions(),
        gate=lambda _a: GateDecision.STOP,
    )
    res_closed = closed_reader.read_page("https://2gis.ru/spb/search/y", page=1)
    assert res_closed.stopped is True
    assert closed.navigations == []
    assert closed.clicks == []


def test_search_reader_404_and_partial_block_preservation() -> None:
    remote = _FakeRemote()
    remote.doc_status = 404
    reader = SearchPageReader(remote, ChromeParserOptions(skip_404_response=True), gate=_go)  # type: ignore[arg-type]
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
    reader2 = SearchPageReader(remote2, ChromeParserOptions(), gate=_go)  # type: ignore[arg-type]
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
    reader = SearchPageReader(remote, ChromeParserOptions(), gate=_go)  # type: ignore[arg-type]
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

    reader = InBuildingPageReader(remote, ChromeParserOptions(), gate=_go)  # type: ignore[arg-type]
    res = reader.read_page("https://2gis.ru/spb/inside/555")
    assert remote.clicks == ["/spb/firm/10", "/spb/firm/20"]
    assert [p["result"]["items"][0]["id"] for p in res.items] == ["10", "20"]
    assert res.has_more is False


async def test_chrome_session_error_translation_and_visible_window() -> None:
    def _no_chrome(_opts: ChromeOptions, _patterns: list[str]) -> Any:
        raise ChromePathNotFound()

    session = ChromeScraperSession(gate=_go, remote_factory=_no_chrome)
    with pytest.raises(ProviderUnavailableError):
        await session.start()

    # The window is always visible.
    assert session._chrome_options.start_maximized is True

    # Offline: no connection, not a temporary error of the site
    offline_remote = _FakeRemote()
    offline_remote.navigate = MagicMock(side_effect=ChromeException("net::ERR_INTERNET_DISCONNECTED"))  # type: ignore[method-assign]
    offline_session = ChromeScraperSession(gate=_go, remote_factory=lambda _o, _p: offline_remote)  # type: ignore[arg-type,return-value]
    await offline_session.start()
    try:
        with pytest.raises(ProviderNoConnectionError):
            await offline_session.read_search("https://2gis.ru/spb/search/x")
    finally:
        await offline_session.close()

    # Generic Chrome failure maps to ProviderTemporaryError
    broken_remote = _FakeRemote()
    broken_remote.navigate = MagicMock(side_effect=ChromeException("CDP command timed out"))  # type: ignore[method-assign]
    broken_session = ChromeScraperSession(gate=_go, remote_factory=lambda _o, _p: broken_remote)  # type: ignore[arg-type,return-value]
    await broken_session.start()
    try:
        with pytest.raises(ProviderTemporaryError):
            await broken_session.read_search("https://2gis.ru/spb/search/x")
    finally:
        await broken_session.close()


def test_chrome_remote_lightweight_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
            self.Runtime.evaluate.return_value = {"result": {"value": None}}
            self._send = MagicMock(return_value={})

        def start(self) -> None:
            return None

        def stop(self) -> None:
            self._stopped.is_set.return_value = True

    fake_tab = _FakeTab()
    monkeypatch.setattr("src.services.scraper.chrome.remote.ChromeBrowser", _FakeBrowserProcess)
    monkeypatch.setattr("src.services.scraper.chrome.remote.patch_pychrome", lambda: None)
    _fake_cdp_modules(monkeypatch, _FakeTab)
    monkeypatch.setattr(ChromeRemote, "_create_tab", lambda self: fake_tab)

    remote = ChromeRemote(ChromeOptions(binary_path=fake_bin), [ITEM_RESPONSE_PATTERN])
    remote.start()
    try:
        fake_tab.Runtime.evaluate.return_value = {"result": {"value": "Стоматологии — 2ГИС"}}
        assert remote.read_title() == "Стоматологии — 2ГИС"
        fake_tab.Runtime.evaluate.return_value = {"result": {"value": 123}}
        assert remote.read_title() == ""

        fake_tab.Runtime.evaluate.return_value = {
            "result": {"value": json.dumps([f"/spb/firm/101?stat={_b64('a')}", f"/spb/station/9?stat={_b64('b')}"])}
        }
        assert remote.read_search_link_hrefs() == [
            f"/spb/firm/101?stat={_b64('a')}",
            f"/spb/station/9?stat={_b64('b')}",
        ]
        fake_tab.Runtime.evaluate.return_value = {"result": {"value": "not-json"}}
        assert remote.read_search_link_hrefs() is None
        fake_tab.Runtime.evaluate.return_value = {"result": {"value": None}}
        assert remote.read_search_link_hrefs() is None

        def fake_navigate(url: str, timeout: int = 15) -> None:
            remote._chrome_tab.Network.requestWillBeSent(
                requestId="req-robots",
                type="Document",
                request={"url": url},
            )
            remote._chrome_tab.Network.responseReceived(
                requestId="req-robots",
                type="Document",
                response={"url": url, "status": 200},
            )

        fake_tab.Runtime.evaluate.side_effect = [
            {"result": {"value": "complete"}},
            {"result": {"value": "User-agent: *\nDisallow: /private\n"}},
        ]
        monkeypatch.setattr(remote, "navigate", fake_navigate)
        status, body = remote.fetch_plain_text("https://2gis.ru/robots.txt", timeout=15)
        assert status == 200
        assert "Disallow: /private" in body
        assert remote.get_requests() == []
    finally:
        remote.stop()


def test_search_reader_uses_lightweight_polling_and_falls_back_cleanly() -> None:
    class _LightRemote(_FakeRemote):
        def __init__(self) -> None:
            super().__init__()
            self.doc_calls = 0
            self.href_calls = 0
            self.title_calls = 0
            self.hrefs_override: list[str] | None = None
            self.quick_find_calls = 0

        def read_title(self) -> str:
            self.title_calls += 1
            return self.current_page_info[1]

        def read_search_link_hrefs(self) -> list[str] | None:
            self.href_calls += 1
            if self.hrefs_override is not None:
                return self.hrefs_override
            doc = self.pages[self.current_page]
            return [node.attributes["href"] for node in doc.search(is_search_result_link)]

        def find_link_by_firm_id(self, firm_id: str) -> DOMNode | None:
            self.quick_find_calls += 1
            return _dom_link(900 + self.quick_find_calls, f"/spb/firm/{firm_id}?stat={_b64('q')}")

        def get_document(self, full: bool = True) -> DOMNode:
            self.doc_calls += 1
            return super().get_document(full=full)

    assert is_search_result_href(f"/spb/firm/101?stat={_b64('a')}")
    assert not is_search_result_href("/spb/firm/101")

    remote = _LightRemote()
    remote.pages = {
        1: _dom_root(
            _dom_link(2, f"/spb/firm/101?stat={_b64('a')}"),
            _dom_link(3, "/spb/search/x/page/2"),
        ),
    }
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=101",
            "body": json.dumps(_byid_payload("101", "Клиника 1")),
        },
    ]
    reader = SearchPageReader(remote, ChromeParserOptions(), gate=_go)  # type: ignore[arg-type]
    res = reader.read_page("https://2gis.ru/spb/search/x", page=1)

    assert [p["result"]["items"][0]["id"] for p in res.items] == ["101"]
    assert remote.href_calls >= 4  # _page_has_search_links_or_empty + 3 stable reads
    # Only ONE get_document() call in _scrape_current_page (no polling DOM reads and no double read).
    assert remote.doc_calls == 1
    assert remote.quick_find_calls == 1

    # Fallback when read_search_link_hrefs returns None
    fallback_remote = _LightRemote()
    fallback_remote.pages = {1: _dom_root(_dom_link(2, f"/spb/firm/102?stat={_b64('b')}"))}
    fallback_remote.hrefs_override = None

    def _none_hrefs() -> list[str] | None:
        fallback_remote.href_calls += 1
        return None

    fallback_remote.read_search_link_hrefs = _none_hrefs  # type: ignore[method-assign]
    fallback_remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=102",
            "body": json.dumps(_byid_payload("102", "Клиника 2")),
        },
    ]
    fallback_reader = SearchPageReader(fallback_remote, ChromeParserOptions(), gate=_go)  # type: ignore[arg-type]
    res_fb = fallback_reader.read_page("https://2gis.ru/spb/search/x", page=1)
    assert [p["result"]["items"][0]["id"] for p in res_fb.items] == ["102"]
    assert fallback_remote.doc_calls >= 5


def test_search_reader_skip_firm_ids_preserves_page_recognition_and_pagination(
    caplog: pytest.LogCaptureFixture,
) -> None:
    remote = _FakeRemote()
    remote.pages = {
        1: _dom_root(
            _dom_link(2, f"/spb/firm/101?stat={_b64('a')}"),
            _dom_link(3, f"/spb/firm/102?stat={_b64('b')}"),
            _dom_link(4, "/spb/search/x/page/2"),
        ),
        2: _dom_root(
            _dom_link(5, f"/spb/firm/201?stat={_b64('c')}"),
        ),
    }
    remote.responses_by_url = [
        {"url": "https://catalog.api.2gis.ru/3.0/items?q=x", "mimeType": "application/json", "body": '{"result":{}}'}
    ]
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=201",
            "body": json.dumps(_byid_payload("201", "Клиника 201")),
        },
    ]

    get_responses_calls = 0

    def _tracked_get_responses() -> list[dict[str, Any]]:
        nonlocal get_responses_calls
        get_responses_calls += 1
        return list(remote.responses_by_url)

    remote.get_responses = _tracked_get_responses  # type: ignore[method-assign]

    gate_actions: list[GateAction] = []

    def _recording_gate(action: GateAction) -> GateDecision:
        gate_actions.append(action)
        return GateDecision.GO

    reader = SearchPageReader(remote, ChromeParserOptions(), gate=_recording_gate)  # type: ignore[arg-type]
    with caplog.at_level("INFO"):
        p1 = reader.read_page("https://2gis.ru/spb/search/x", page=1, skip_firm_ids={"101", "102"})

    assert p1.items == []
    assert p1.links_found == 2
    assert p1.has_more is True
    assert remote.clicks == []
    assert [a.kind for a in gate_actions] == ["open"]
    assert get_responses_calls == 0
    assert "Search page 1: skipped 2 already-known organizations" in caplog.text

    p2 = reader.read_page("https://2gis.ru/spb/search/x", page=2, skip_firm_ids={"101", "102"})
    assert [p["result"]["items"][0]["id"] for p in p2.items] == ["201"]
    assert p2.has_more is False


async def test_chrome_session_fetch_text_resets_search_reader() -> None:
    remote = _FakeRemote()
    remote.pages = {1: _dom_root(_dom_link(2, f"/spb/firm/101?stat={_b64('a')}"))}
    remote.click_outcomes = [
        {
            "status": 200,
            "url": "https://catalog.api.2gis.ru/3.0/items/byid?id=101",
            "body": json.dumps(_byid_payload("101", "Клиника 1")),
        }
    ]

    def _fetch_plain_text(url: str, timeout: int = 15) -> tuple[int | None, str]:
        return 200, f"robots from {url}"

    remote.fetch_plain_text = _fetch_plain_text  # type: ignore[attr-defined]
    session = ChromeScraperSession(
        gate=_go,
        remote_factory=lambda _o, _p: remote,  # type: ignore[arg-type,return-value]
    )
    await session.start()
    try:
        await session.read_search("https://2gis.ru/spb/search/x", page=1)
        assert session._search_reader is not None
        assert session._search_reader._current_page == 1
        status, text = await session.fetch_text("https://2gis.ru/robots.txt")
        assert status == 200
        assert text == "robots from https://2gis.ru/robots.txt"
        assert session._search_reader._current_page == 0
    finally:
        await session.close()
