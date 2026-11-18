"""
Chrome DevTools Protocol wrapper built on pychrome, ported from parser-2gis-new.

Only reads what pages show and receive, navigates and calls element.click(). Nothing is injected
into pages and no browser property is overridden. The timezone and the language only follow the place
of the proxy; the user agent and everything else a page can ask the browser stay as they are.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import queue
import re
import threading
import warnings
from typing import TYPE_CHECKING, Any

from src.services.api_client import MAX_RESPONSE_BYTES

from .browser import ChromeBrowser
from .dom import DOMNode
from .exceptions import ChromeException, ChromeRuntimeException, ProxyError
from .filters import HEAVY_RESOURCE_PATTERNS
from .proxy import IP_CHECK_SERVICES, ProxySettings, extract_ip_info, parse_proxy
from .utils import ensure_local_cdp_url, wait_until_finished

if TYPE_CHECKING:
    from .options import ChromeOptions

RequestDict = dict[str, Any]
ResponseDict = dict[str, Any]

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS: int = 10
_CONNECT_TIMEOUT_SECONDS: int = 60
_DEFAULT_STATE_VARS: tuple[str, ...] = ("initialState", "__REACT_QUERY_STATE__")
# Chrome asks again when the login is wrong: after so many answers the proxy counts as refusing it.
_PROXY_AUTH_MAX_ATTEMPTS: int = 20
_IP_CHECK_TIMEOUT_SECONDS: int = 30
_pychrome_patched = False


def patch_pychrome() -> None:
    """Patch `pychrome.Tab._recv_loop` so empty WebSocket frames do not crash JSON parsing."""
    global _pychrome_patched
    if _pychrome_patched:
        return

    import pychrome.tab
    import websocket

    pychrome_logger = logging.getLogger("pychrome")

    def _recv_loop_patched(self: Any) -> None:
        while not self._stopped.is_set():
            try:
                self._ws.settimeout(1)
                message_json = self._ws.recv()
                if not message_json:
                    continue
                message = json.loads(message_json)
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError):
                if not self._stopped.is_set():
                    pychrome_logger.error("websocket exception", exc_info=True)
                    self._stopped.set()
                return

            if self.debug:  # pragma: no cover
                print(f"< RECV {message_json}")

            if "method" in message:
                self.event_queue.put(message)
            elif "id" in message:
                if message["id"] in self.method_results:
                    self.method_results[message["id"]].put(message)
            else:  # pragma: no cover
                warnings.warn(f"unknown message: {message}", stacklevel=2)

    pychrome.tab.Tab._recv_loop = _recv_loop_patched
    _pychrome_patched = True


class ChromeRemote:
    """Wrapper for Chrome DevTools Protocol tab lifecycle, interception, and DOM actions."""

    def __init__(self, chrome_options: ChromeOptions, response_patterns: list[str]) -> None:
        self._chrome_options = chrome_options
        self._chrome_browser: ChromeBrowser | None = None
        self._chrome_interface: Any = None
        self._chrome_tab: Any = None
        self._dev_url: str = ""
        self._response_patterns = list(response_patterns)
        self._response_queues: dict[str, queue.Queue[ResponseDict]] = {
            pattern: queue.Queue() for pattern in self._response_patterns
        }
        self._requests: dict[str, RequestDict] = {}
        self._requests_lock = threading.Lock()
        self._ping_thread: threading.Thread | None = None
        self._proxy: ProxySettings | None = (
            parse_proxy(chrome_options.proxy.get_secret_value()) if chrome_options.proxy is not None else None
        )
        self._proxy_auth_attempts = 0
        self._proxy_auth_failed = False
        # What the check before the first request found out: the IP the sites see, and its country.
        self.exit_ip: str | None = None
        self.exit_country: str | None = None
        # How many requests for heavy resources the browser refused (the light mode), for the log.
        self.blocked_count = 0

    @wait_until_finished(timeout=_CONNECT_TIMEOUT_SECONDS)
    def _connect_interface(self) -> bool:
        import pychrome
        from requests.exceptions import RequestException
        from websocket import WebSocketException

        if self._chrome_browser is not None and self._chrome_browser.returncode is not None:
            raise ChromeRuntimeException(f"Chrome завершил работу при запуске (код {self._chrome_browser.returncode})")

        try:
            self._chrome_interface = pychrome.Browser(url=self._dev_url)
            self._chrome_tab = self._create_tab()
            self._chrome_tab.start()
            return True
        except (RequestException, WebSocketException, ValueError):
            if self._chrome_browser is not None and self._chrome_browser.returncode is not None:
                raise ChromeRuntimeException(
                    f"Chrome завершил работу при запуске (код {self._chrome_browser.returncode})"
                ) from None
            return False

    def start(self) -> None:
        """Start Chrome, attach a new DevTools tab, and record the network responses of its pages."""
        patch_pychrome()
        self._chrome_browser = ChromeBrowser(self._chrome_options)
        try:
            self._dev_url = ensure_local_cdp_url(f"http://127.0.0.1:{self._chrome_browser.remote_port}")
            self._connect_interface()
            self._setup_tab()
            self._init_tab_monitor()
            # Before 2GIS is touched at all: from the wrong country nothing may be opened.
            if self._proxy is not None or self._chrome_options.country_to_check is not None:
                self._check_exit_ip()
        except Exception:
            self.stop()
            raise

    @wait_until_finished(timeout=20, throw_exception=False)
    def _wait_document_ready(self) -> bool:
        return self.execute_script("document.readyState") == "complete"

    def _check_exit_ip(self) -> None:
        """
        Open an IP service in the browser and compare the country of the exit IP with the required one.

        Raises ProxyError if the proxy does not answer or refuses the login, or the country is wrong.
        """
        required = self._chrome_options.country_to_check
        proxy_name = self._proxy.masked() if self._proxy is not None else "без прокси"
        problems: list[str] = []

        for url, country_field, ip_field in IP_CHECK_SERVICES:
            try:
                self.navigate(url, timeout=_IP_CHECK_TIMEOUT_SECONDS)
            except ChromeException as exc:
                problems.append(f"{url.split('/')[2]}: {exc}")
                if self._proxy_auth_failed:
                    break
                continue
            self._wait_document_ready()
            body = self.execute_script('document.body ? document.body.innerText : ""')
            ip, country = extract_ip_info(str(body or ""), country_field, ip_field)
            if country:
                self.exit_ip, self.exit_country = ip, country
                break
            problems.append(f"{url.split('/')[2]}: непонятный ответ")

        # The check is not a page of 2GIS: what it loaded must not be taken for one.
        self.clear_requests()

        if self._proxy_auth_failed:
            raise ProxyError(f"Прокси {proxy_name} не принял логин или пароль")
        if self.exit_country is None:
            if self._proxy is not None:
                # With a proxy, "nothing loads" almost always means that the proxy is dead.
                raise ProxyError(f"Не удалось выйти в интернет через прокси {proxy_name}: {'; '.join(problems)}")
            raise ProxyError(
                f"Не удалось узнать страну внешнего IP ({'; '.join(problems)}). "
                "Проверьте сеть или отключите проверку: TWOGIS_PROXY_COUNTRY=off"
            )

        logger.info("External IP: %s (%s), %s", self.exit_ip, self.exit_country, proxy_name)
        if required is not None and self.exit_country != required:
            raise ProxyError(
                f"Внешний IP {self.exit_ip} из страны {self.exit_country}, а нужна {required} ({proxy_name}). "
                "Сбор остановлен, чтобы не ходить в 2ГИС с неподходящего IP"
            )

    def _create_tab(self) -> Any:
        import pychrome
        import requests

        resp = requests.put(f"{self._dev_url}/json/new", timeout=_HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 405:
            resp = requests.get(f"{self._dev_url}/json/new", timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return pychrome.Tab(**resp.json())

    def _close_tab(self, tab: Any) -> None:
        import pychrome
        import requests

        if tab.status == pychrome.Tab.status_started:
            tab.stop()
        requests.put(f"{self._dev_url}/json/close/{tab.id}", timeout=_HTTP_TIMEOUT_SECONDS)

    def _setup_tab(self) -> None:
        """Subscribe to CDP Network events. Nothing about the page or the browser is changed."""

        def response_received(**kwargs: Any) -> None:
            response = dict(kwargs.pop("response"))
            response["meta"] = kwargs
            request_id = kwargs["requestId"]
            if kwargs.get("type") == "Preflight":
                return

            with self._requests_lock:
                if request_id in self._requests:
                    request = self._requests[request_id]
                    response["request"] = request
                    request["response"] = response

            url = str(response.get("url") or "")
            for pattern in self._response_patterns:
                if re.match(pattern, url):
                    self._response_queues[pattern].put(response)

        def loading_failed(**kwargs: Any) -> None:
            if kwargs.get("blockedReason") == "inspector":
                self.blocked_count += 1
                return  # our own refusal, not a failure of the site
            error_text = kwargs.get("errorText")
            blocked_reason = kwargs.get("blockedReason")
            bits = []
            if error_text:
                bits.append(f"error: {error_text}")
            if blocked_reason:
                bits.append(f"blocked_reason: {blocked_reason}")

            request_id = kwargs.get("requestId")
            response: ResponseDict = {"status": -1, "statusText": ", ".join(bits), "meta": kwargs}

            request_url: str | None = None
            with self._requests_lock:
                if request_id in self._requests:
                    request = self._requests[request_id]
                    response["request"] = request
                    request["response"] = response
                    request_url = str(request.get("url") or "")

            if request_url:
                for pattern in self._response_patterns:
                    if re.match(pattern, request_url):
                        self._response_queues[pattern].put(response)

        def request_will_be_sent(**kwargs: Any) -> None:
            request = dict(kwargs.pop("request"))
            request["meta"] = kwargs
            request_id = kwargs["requestId"]
            if kwargs.get("type") == "Preflight":
                return
            with self._requests_lock:
                self._requests[request_id] = request

        self._chrome_tab.Network.responseReceived = response_received
        self._chrome_tab.Network.loadingFailed = loading_failed
        self._chrome_tab.Network.requestWillBeSent = request_will_be_sent

        def fetch_request_paused(**kwargs: Any) -> None:
            """Fetch is on only to answer the proxy: every request goes on as it was."""
            with contextlib.suppress(Exception):  # a cancelled request or a closing tab
                self._chrome_tab.Fetch.continueRequest(requestId=kwargs["requestId"])

        def fetch_auth_required(**kwargs: Any) -> None:
            """The proxy asks for the login: it is given to the proxy only, never to a site."""
            challenge = kwargs.get("authChallenge") or {}
            proxy = self._proxy
            if challenge.get("source") == "Proxy" and proxy is not None and not self._proxy_auth_failed:
                self._proxy_auth_attempts += 1
                if self._proxy_auth_attempts <= _PROXY_AUTH_MAX_ATTEMPTS:
                    answer = {
                        "response": "ProvideCredentials",
                        "username": proxy.username,
                        "password": proxy.password,
                    }
                else:
                    self._proxy_auth_failed = True
                    answer = {"response": "CancelAuth"}
            else:
                answer = {"response": "Default"}
            with contextlib.suppress(Exception):
                self._chrome_tab.Fetch.continueWithAuth(requestId=kwargs["requestId"], authChallengeResponse=answer)

        self._chrome_tab.Network.enable()
        self._chrome_tab.DOM.enable()
        self._chrome_tab.Page.enable()
        self._chrome_tab.Runtime.enable()
        self._chrome_tab.Log.enable()
        self._apply_locale()
        self._block_heavy_resources()

        # Chrome ignores the login in --proxy-server, so the proxy's request for it is answered here.
        if self._proxy is not None and self._proxy.has_auth:
            self._chrome_tab.Fetch.requestPaused = fetch_request_paused
            self._chrome_tab.Fetch.authRequired = fetch_auth_required
            self._chrome_tab.Fetch.enable(handleAuthRequests=True, patterns=[{"urlPattern": "*"}])

    def _block_heavy_resources(self) -> None:
        """The light mode: the browser does not even ask for what only draws the page."""
        if not self._chrome_options.light:
            return
        import pychrome

        try:
            self._chrome_tab.Network.setBlockedURLs(urls=list(HEAVY_RESOURCE_PATTERNS))
        except pychrome.CallMethodException as exc:
            logger.warning("Could not block the heavy resources, the browser stays heavier: %s", exc)

    def _apply_locale(self) -> None:
        """The timezone of the browser follows the place of the proxy; in headless mode the window gets a size."""
        import pychrome

        timezone = self._chrome_options.timezone
        if timezone:
            try:
                self._chrome_tab.Emulation.setTimezoneOverride(timezoneId=timezone)
            except pychrome.CallMethodException as exc:
                logger.warning("Could not set the timezone %s: %s", timezone, exc)
        if self._chrome_options.headless:
            # A tab made over DevTools has a tiny window, and the map of 2GIS then collapses to a point.
            self._chrome_tab.Emulation.setDeviceMetricsOverride(
                width=1920, height=1080, deviceScaleFactor=1, mobile=False
            )

    def _init_tab_monitor(self) -> None:
        """Detect V8 OOM tab crashes that leave WebSocket open until next command."""
        import pychrome
        import requests
        from requests.exceptions import RequestException

        tab = self._chrome_tab
        dev_url = self._dev_url
        tab_detached = False

        def monitor_tab() -> None:
            nonlocal tab_detached
            while not tab._stopped.is_set():
                try:
                    ret = requests.get(f"{dev_url}/json", timeout=_HTTP_TIMEOUT_SECONDS)
                    tabs = ret.json()
                    if not any(entry.get("id") == tab.id for entry in tabs):
                        tab_detached = True
                        tab._stopped.set()
                        break
                    tab._stopped.wait(0.5)
                except (RequestException, ValueError):
                    break

        self._ping_thread = threading.Thread(target=monitor_tab, daemon=True)
        self._ping_thread.start()

        original_send = tab._send

        def wrapped_send(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_send(*args, **kwargs)
            except pychrome.UserAbortException as exc:
                if tab_detached:
                    raise ChromeRuntimeException("Tab has been stopped") from exc
                raise ChromeRuntimeException("CDP tab connection stopped") from exc
            except pychrome.RuntimeException as exc:
                raise ChromeRuntimeException(str(exc)) from exc

        tab._send = wrapped_send

    def _require_tab(self) -> Any:
        if self._chrome_tab is None or self._chrome_tab._stopped.is_set():
            raise ChromeRuntimeException("окно браузера закрыто")
        return self._chrome_tab

    def navigate(self, url: str, timeout: int = 60) -> None:
        """Navigate tab to `url`, as typed into the address bar: no referrer."""
        tab = self._require_tab()
        ret = tab.Page.navigate(url=url, _timeout=timeout)
        error_message = ret.get("errorText") if isinstance(ret, dict) else None
        if error_message:
            raise ChromeException(str(error_message))

    @wait_until_finished(timeout=30, throw_exception=False)
    def wait_response(self, response_pattern: str) -> ResponseDict | None:
        """Pop the next intercepted response matching `response_pattern`."""
        tab = self._require_tab()
        if tab._stopped.is_set():
            raise ChromeRuntimeException("Tab has been stopped")
        target_queue = self._response_queues.get(response_pattern)
        if target_queue is None:
            return None
        try:
            return target_queue.get(block=False)
        except queue.Empty:
            return None

    def drain_responses(self, response_pattern: str) -> None:
        """Discard any unread responses queued for `response_pattern` before triggering a new click."""
        target_queue = self._response_queues.get(response_pattern)
        if target_queue is None:
            return
        while True:
            try:
                target_queue.get_nowait()
            except queue.Empty:
                return

    def clear_requests(self) -> None:
        """Release recorded network request/response objects."""
        with self._requests_lock:
            self._requests.clear()

    @wait_until_finished(timeout=15, throw_exception=False)
    def get_response_body(self, response: ResponseDict) -> str:
        """Fetch decoded UTF-8 body of an intercepted CDP network response."""
        import pychrome

        tab = self._require_tab()
        try:
            request_id = response["meta"]["requestId"]
            response_data = tab.call_method("Network.getResponseBody", requestId=request_id)
            body = response_data["body"]
            if response_data.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            response["body"] = body
            return str(body)
        except (pychrome.CallMethodException, KeyError):
            return ""

    @wait_until_finished(timeout=None, throw_exception=False)
    def get_responses(self) -> list[ResponseDict]:
        """Collect all recorded responses so far."""
        with self._requests_lock:
            return [entry["response"] for entry in self._requests.values() if "response" in entry]

    @wait_until_finished(timeout=5, throw_exception=False)
    def get_document_response(self) -> ResponseDict | None:
        """Wait for the primary Document response from the current navigation."""
        with self._requests_lock:
            responses = [entry["response"] for entry in self._requests.values() if "response" in entry]
        for response in reversed(responses):
            meta = response.get("meta")
            if isinstance(meta, dict) and meta.get("type") == "Document":
                return response
        return responses[-1] if responses else None

    def get_requests(self) -> list[RequestDict]:
        """Return snapshot of all recorded requests."""
        with self._requests_lock:
            return list(self._requests.values())

    def get_document(self, full: bool = True) -> DOMNode:
        """Retrieve current DOM tree from Chrome."""
        tab = self._require_tab()
        tree = tab.DOM.getDocument(depth=-1 if full else 1)
        return DOMNode(**tree["root"])

    def execute_script(self, expression: str) -> Any:
        """Evaluate JavaScript expression in page context and return its JSON value."""
        tab = self._require_tab()
        eval_result = tab.Runtime.evaluate(expression=expression, returnByValue=True)
        return eval_result.get("result", {}).get("value")

    def perform_click(self, dom_node: DOMNode, timeout: int | None = None) -> None:
        """Scroll `dom_node` into center view and call its element.click(). No input events are synthesized."""
        tab = self._require_tab()
        resolved_node = tab.DOM.resolveNode(backendNodeId=dom_node.backend_id, _timeout=timeout)
        object_id = resolved_node["object"]["objectId"]
        tab.Runtime.callFunctionOn(
            objectId=object_id,
            functionDeclaration=(
                'function() { this.scrollIntoView({ block: "center", behavior: "instant" }); this.click(); }'
            ),
        )

    def bring_to_front(self) -> None:
        """Bring the browser window/tab to foreground for manual captcha passing."""
        tab = self._require_tab()
        tab.Page.bringToFront()

    def read_current_page(self) -> tuple[str, str, str]:
        """Atomically read `(url, title, html)` of the active document."""
        raw = self.execute_script(
            "JSON.stringify({"
            "url: String(location.href || ''), "
            "title: String(document.title || ''), "
            "html: document.documentElement ? document.documentElement.outerHTML : ''"
            "})"
        )
        if not isinstance(raw, str):
            return "", "", ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return "", "", ""
        return str(parsed.get("url") or ""), str(parsed.get("title") or ""), str(parsed.get("html") or "")

    def read_page_state(self, names: tuple[str, ...] = _DEFAULT_STATE_VARS) -> tuple[dict[str, Any], dict[str, str]]:
        """Read serialized global state variables (`initialState`, `__REACT_QUERY_STATE__`)."""
        expr = (
            f"((names) => JSON.stringify(names.map((name) => {{"
            f"try {{"
            f"const text = JSON.stringify(globalThis[name]);"
            f"return text === undefined ? {{problem: 'нет такой переменной'}} : {{json: text}};"
            f"}} catch (error) {{"
            f"return {{problem: 'значение не сериализуется в JSON'}};"
            f"}}"
            f"}})))({json.dumps(list(names))})"
        )
        raw = self.execute_script(expr)
        if not isinstance(raw, str):
            return {}, dict.fromkeys(names, "страница не дала прочитать")
        try:
            answers = json.loads(raw)
        except ValueError:
            return {}, dict.fromkeys(names, "страница не дала прочитать")

        values: dict[str, Any] = {}
        problems: dict[str, str] = {}
        for name, answer in zip(names, answers, strict=True):
            text = answer.get("json")
            if text is None:
                problems[name] = str(answer.get("problem"))
            elif len(text) > MAX_RESPONSE_BYTES:
                problems[name] = "значение слишком большое"
            else:
                values[name] = json.loads(text)
        return values, problems

    def wait(self, timeout: float | None = None) -> None:
        """Sleep using pychrome tab wait loop."""
        tab = self._require_tab()
        tab.wait(timeout)

    def stop(self) -> None:
        """Close CDP tab, stop Chrome browser process, and release queues."""
        from requests.exceptions import RequestException

        if self.blocked_count:
            logger.info("The browser refused %d requests for heavy resources", self.blocked_count)
        tab, self._chrome_tab = self._chrome_tab, None
        browser, self._chrome_browser = self._chrome_browser, None
        self._chrome_interface = None

        if tab is not None:
            try:
                self._close_tab(tab)
            except (ChromeException, RequestException, OSError):
                logger.debug("Closing CDP tab failed cleanly", exc_info=True)

        if browser is not None:
            try:
                browser.close()
            except OSError:
                logger.warning("Closing Chrome browser process failed", exc_info=True)

        self.clear_requests()
        for q in self._response_queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def __enter__(self) -> ChromeRemote:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(options={self._chrome_options!r}, "
            f"response_patterns={self._response_patterns!r})"
        )
