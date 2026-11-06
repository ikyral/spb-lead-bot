"""
A Chromium session. Only this module touches Playwright.

The browser runs with its defaults: the standard User-Agent (headless it says 'HeadlessChrome'),
no proxies, no changes to the fingerprint and nothing that hides automation. This is intentional.
With a profile folder it keeps cookies between runs, like any browser someone uses; nothing here
acts on a page on the owner's behalf, it only opens pages and reads what they show. That includes
the variables a page keeps its data in: reading them changes nothing and sends nothing to the site.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.api_client import MAX_RESPONSE_BYTES, ProviderTemporaryError, ProviderUnavailableError

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response

logger = logging.getLogger(__name__)

# A stuck close must not hold up the bot's shutdown.
CLOSE_TIMEOUT_SECONDS: float = 10.0
# Pages keep loading the map for a long time; the data of interest arrives well before that.
NETWORK_IDLE_TIMEOUT_SECONDS: float = 15.0
# Reading what a page shows must not hang the wait for a person.
READ_PAGE_TIMEOUT_SECONDS: float = 10.0
# Where pages of 2GIS keep the data they arrive with.
PAGE_STATE_VARIABLES: tuple[str, ...] = ("initialState", "__REACT_QUERY_STATE__")
# Only reads. A string comes back, so what is too big or cannot be serialized does not cross over half-way.
_READ_STATE_SCRIPT: str = """(names) => names.map((name) => {
  try {
    const text = JSON.stringify(globalThis[name]);
    return text === undefined ? {problem: "нет такой переменной"} : {json: text};
  } catch (error) {
    return {problem: "значение не сериализуется в JSON"};
  }
})"""


@dataclass
class PageSnapshot:
    """What a page showed: everything the site-specific code needs, no browser objects."""

    status: int | None
    final_url: str
    title: str
    html: str
    json_bodies: list[dict[str, Any]] = field(default_factory=list)
    # Where each of json_bodies came from, in the same order.
    json_urls: list[str] = field(default_factory=list)
    # The values of PAGE_STATE_VARIABLES, and why those that are missing here could not be read.
    page_state: dict[str, Any] = field(default_factory=dict)
    page_state_problems: dict[str, str] = field(default_factory=dict)

    def bodies_from(self, wanted: Callable[[str], bool]) -> list[dict[str, Any]]:
        """The bodies that came from the addresses of interest."""
        return [body for url, body in zip(self.json_urls, self.json_bodies, strict=True) if wanted(url)]


class PlaywrightSession:
    """One browser, one context, one page: requests can only go one at a time."""

    def __init__(
        self, page_timeout_seconds: float = 45.0, headless: bool = True, profile_dir: Path | None = None
    ) -> None:
        self._timeout_ms = page_timeout_seconds * 1000
        self._headless = headless
        self._profile_dir = profile_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        """Start the browser. Whatever was started before a failure is closed again."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            if self._profile_dir is None:
                self._browser = await self._playwright.chromium.launch(headless=self._headless)
                self._context = await self._browser.new_context(locale="ru-RU")
                self._page = await self._context.new_page()
            else:
                await asyncio.to_thread(self._profile_dir.mkdir, parents=True, exist_ok=True)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(self._profile_dir), headless=self._headless, locale="ru-RU"
                )
                # A persistent context opens with a window of its own.
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
        except (PlaywrightError, OSError) as exc:
            await self.close()
            logger.error("Chromium failed to start: %s", exc)
            hint = "браузер не запустился. Установите его командой: playwright install chromium"
            if self._profile_dir is not None:
                hint += f". Проверьте также, что папку профиля {self._profile_dir} не занял другой запущенный сборщик"
            raise ProviderUnavailableError(hint) from None

    async def visit(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot:
        """Open a URL and collect JSON responses the page itself received, if `wants_body` accepts their URL."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        page = self._page
        if page is None:
            raise ProviderUnavailableError("браузер не запущен")

        reads: list[asyncio.Future[dict[str, Any] | None]] = []
        urls: list[str] = []

        def on_response(response: Response) -> None:
            if wants_body(response.url):
                urls.append(response.url)
                reads.append(asyncio.ensure_future(_read_json(response)))

        page.on("response", on_response)
        listening = True
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            with suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_SECONDS * 1000)
            # A page goes on receiving responses during the wait: only those that came by now are waited for.
            pending = list(zip(urls, reads, strict=True))
            bodies = await asyncio.gather(*(read for _, read in pending))
            page.remove_listener("response", on_response)
            listening = False
            read_bodies = [
                (body_url, body) for (body_url, _), body in zip(pending, bodies, strict=True) if body is not None
            ]
            page_state, page_state_problems = await _read_page_state(page)
            return PageSnapshot(
                status=response.status if response is not None else None,
                final_url=page.url,
                title=await page.title(),
                html=await page.content(),
                json_bodies=[body for _, body in read_bodies],
                json_urls=[body_url for body_url, _ in read_bodies],
                page_state=page_state,
                page_state_problems=page_state_problems,
            )
        except PlaywrightTimeoutError:
            raise ProviderTemporaryError("страница не загрузилась вовремя") from None
        except PlaywrightError as exc:
            # The class only: Playwright's messages contain the URL.
            raise ProviderTemporaryError(f"ошибка браузера ({type(exc).__name__})") from None
        finally:
            if listening:
                page.remove_listener("response", on_response)
            for read in reads:
                read.cancel()

    async def read_page(self) -> PageSnapshot | None:
        """
        What the window shows right now, without navigating anywhere.

        None while it cannot be read, for example during a navigation. Raises
        ProviderUnavailableError when the window is gone: waiting for it would be pointless.
        """
        from playwright.async_api import Error as PlaywrightError

        page = self._page
        if page is None or page.is_closed():
            raise ProviderUnavailableError("окно браузера закрыто")

        async def read() -> PageSnapshot:
            return PageSnapshot(status=None, final_url=page.url, title=await page.title(), html=await page.content())

        try:
            return await asyncio.wait_for(read(), READ_PAGE_TIMEOUT_SECONDS)
        except (PlaywrightError, TimeoutError):
            return None

    async def bring_to_front(self) -> None:
        """Ask for the window to come forward. The OS may only flash it in the taskbar."""
        from playwright.async_api import Error as PlaywrightError

        if self._page is not None:
            try:
                await asyncio.wait_for(self._page.bring_to_front(), READ_PAGE_TIMEOUT_SECONDS)
            except (PlaywrightError, TimeoutError):
                logger.info("The browser window could not be brought to the front")

    async def close(self) -> None:
        """Close everything, each step independently, even while being cancelled."""
        steps: list[tuple[str, Callable[[], Awaitable[None]] | None]] = [
            ("page", self._page.close if self._page else None),
            ("context", self._context.close if self._context else None),
            ("browser", self._browser.close if self._browser else None),
            # Stopping the driver also ends a browser process that failed to close above.
            ("playwright", self._playwright.stop if self._playwright else None),
        ]
        self._page = self._context = self._browser = self._playwright = None

        cancelled: asyncio.CancelledError | None = None
        for name, step in steps:
            if step is None:
                continue
            try:
                await asyncio.wait_for(step(), CLOSE_TIMEOUT_SECONDS)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                logger.warning("Closing the browser %s failed", name, exc_info=True)
        if cancelled is not None:
            raise cancelled


async def _read_page_state(page: Page) -> tuple[dict[str, Any], dict[str, str]]:
    """The variables of a page. A page that does not give them is still a page: this only tells why."""
    from playwright.async_api import Error as PlaywrightError

    names = PAGE_STATE_VARIABLES
    try:
        answers = await asyncio.wait_for(page.evaluate(_READ_STATE_SCRIPT, list(names)), READ_PAGE_TIMEOUT_SECONDS)
    except (PlaywrightError, TimeoutError) as exc:
        return {}, dict.fromkeys(names, f"страница не дала прочитать ({type(exc).__name__})")

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


async def _read_json(response: Response) -> dict[str, Any] | None:
    """A response that cannot be read or is not a JSON object is simply not used."""
    from playwright.async_api import Error as PlaywrightError

    try:
        body = await response.body()
    except PlaywrightError:
        return None
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
