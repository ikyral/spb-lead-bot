"""
A Chromium session. Only this module touches Playwright.

The browser runs with its defaults: the standard User-Agent (headless it says 'HeadlessChrome'),
no proxies, no changes to the fingerprint and nothing that hides automation. This is intentional.
With a profile folder it keeps cookies between runs, like any browser someone uses; nothing here
acts on a page on the owner's behalf, it only opens pages and reads what they show.
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


@dataclass
class PageSnapshot:
    """What a page showed: everything the site-specific code needs, no browser objects."""

    status: int | None
    final_url: str
    title: str
    html: str
    json_bodies: list[dict[str, Any]] = field(default_factory=list)


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

        def on_response(response: Response) -> None:
            if wants_body(response.url):
                reads.append(asyncio.ensure_future(_read_json(response)))

        page.on("response", on_response)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            with suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_SECONDS * 1000)
            bodies = await asyncio.gather(*reads)
            return PageSnapshot(
                status=response.status if response is not None else None,
                final_url=page.url,
                title=await page.title(),
                html=await page.content(),
                json_bodies=[body for body in bodies if body is not None],
            )
        except PlaywrightTimeoutError:
            raise ProviderTemporaryError("страница не загрузилась вовремя") from None
        except PlaywrightError as exc:
            # The class only: Playwright's messages contain the URL.
            raise ProviderTemporaryError(f"ошибка браузера ({type(exc).__name__})") from None
        finally:
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
