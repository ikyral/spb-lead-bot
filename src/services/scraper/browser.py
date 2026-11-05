"""
A headless Chromium session with stealth configuration and free reCAPTCHA v2 checkbox bypass.
Only this module touches Playwright.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from src.services.api_client import MAX_RESPONSE_BYTES, ProviderTemporaryError, ProviderUnavailableError

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response

logger = logging.getLogger(__name__)

# A stuck close must not hold up the bot's shutdown.
CLOSE_TIMEOUT_SECONDS: float = 10.0
# Pages keep loading the map for a long time; the data of interest arrives well before that.
NETWORK_IDLE_TIMEOUT_SECONDS: float = 15.0

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-default-browser-check",
    "--no-first-run",
]

STEALTH_INIT_SCRIPT: str = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
}
"""


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

    def __init__(self, page_timeout_seconds: float = 45.0, headless: bool = True) -> None:
        self._timeout_ms = page_timeout_seconds * 1000
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        """Start the browser with stealth settings. Whatever was started before a failure is closed again."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=STEALTH_ARGS,
            )
            self._context = await self._browser.new_context(
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            await self._context.add_init_script(STEALTH_INIT_SCRIPT)
            self._page = await self._context.new_page()
        except PlaywrightError as exc:
            await self.close()
            logger.error("Chromium failed to start: %s", exc)
            raise ProviderUnavailableError(
                "браузер не запустился. Установите его командой: playwright install chromium"
            ) from None

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

            if isinstance(page.url, str) and _is_captcha_url(page.url):
                logger.info("2GIS captcha detected, attempting free checkbox bypass...")
                bypassed_response = await self._try_solve_captcha(page, target_url=url)
                if bypassed_response is not None:
                    response = bypassed_response

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

    async def _try_solve_captcha(self, page: Page, target_url: str) -> Response | None:
        """Click the reCAPTCHA v2 anchor checkbox and submit `/form` if Google grants a one-click pass."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            await asyncio.sleep(random.uniform(0.8, 1.5))  # noqa: S311
            await page.mouse.move(random.randint(150, 450), random.randint(150, 350), steps=10)  # noqa: S311

            anchor_frame = page.frame_locator("iframe[src*='recaptcha/api2/anchor']")
            checkbox = anchor_frame.locator("#recaptcha-anchor")
            await checkbox.wait_for(state="visible", timeout=5000)
            await checkbox.click(delay=random.randint(60, 140))  # noqa: S311

            for _ in range(8):
                await asyncio.sleep(0.5)
                if await checkbox.get_attribute("aria-checked") == "true":
                    submit_btn = page.locator("form[action='/form'] input[type='submit']")
                    await submit_btn.click()
                    with suppress(PlaywrightTimeoutError):
                        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_SECONDS * 1000)
                    if not _is_captcha_url(page.url):
                        logger.info("2GIS captcha solved via checkbox click.")
                        return await page.goto(target_url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                    break
        except (PlaywrightError, TimeoutError, AttributeError, TypeError):
            logger.debug("Automatic captcha checkbox bypass did not succeed")
        return None

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


def _is_captcha_url(url: str) -> bool:
    return urlsplit(url).netloc.startswith("captcha.2gis.")


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
