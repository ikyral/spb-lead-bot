"""
The single way to call external map APIs: rate limit, timeouts, retries, response size cap.

Errors are raised as ProviderError subclasses. Their messages are safe to log and to show
to the admin: they never contain a request URL (API keys travel in query strings), and
configured secrets are masked in anything the remote side echoes back.
"""

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: float = 10.0
DEFAULT_MAX_ATTEMPTS: int = 3
BACKOFF_BASE_SECONDS: float = 1.0
# A server may ask to wait longer than a run should block; such a wait is capped.
MAX_RETRY_DELAY_SECONDS: float = 30.0
MAX_RESPONSE_BYTES: int = 10 * 1024 * 1024

_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_AUTH_STATUSES: frozenset[int] = frozenset({401, 403})
_QUOTA_STATUSES: frozenset[int] = frozenset({402, 429})

Sleep = Callable[[float], Awaitable[None]]
# Extracts (status, details) reported inside a JSON body, e.g. meta.code of the 2GIS API.
BodyInspector = Callable[[dict[str, Any]], tuple[int | None, str]]


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Loading CA certificates blocks the event loop for ~150 ms: do it once, not per request."""
    return httpx.create_ssl_context()


FORMAT_NOT_RECOGNIZED_TEXT: str = "Формат страницы не распознан: нужна правка парсера, это не блокировка"
NO_CONNECTION_TEXT: str = "Нет связи с сайтом 2ГИС: проверьте сеть и VPN, это не засчитано как посещение"


class ProviderError(Exception):
    """A request to an external API failed."""


class ProviderTemporaryError(ProviderError):
    """Network problem or a server-side error; the next run may succeed."""


class ProviderResponseError(ProviderError):
    """The request was rejected or the response cannot be used; repeating it will not help."""


class ProviderAuthError(ProviderError):
    """The API key is missing, wrong, blocked or has no access. Needs the owner's attention."""


class ProviderQuotaError(ProviderError):
    """The request limit or quota is exhausted. Needs the owner's attention."""


class ProviderUnavailableError(ProviderError):
    """The provider cannot work right now and repeating will not help (no browser, robots.txt forbids)."""


class ProviderBudgetExhausted(ProviderError):
    """The provider has used up its own limit for this run. Not a failure."""


class ProviderStoppedError(ProviderError):
    """Reading stopped for a reason that says nothing about how the site treats the bot. Not a block."""

    def __init__(self, reason: str, partial: list[Any] | None = None) -> None:
        super().__init__(reason)
        self.partial = partial or []


class ProviderFormatError(ProviderStoppedError):
    """A page loaded normally, but the code does not understand what is in it."""


class ProviderNoConnectionError(ProviderStoppedError):
    """The site did not answer anything at all: the problem is on this side."""


class ProviderBlockedError(ProviderError):
    """
    The site refused automated access: captcha, 403, 429 or a page that came empty.

    `partial` holds what was received before the refusal.
    """

    def __init__(self, reason: str, partial: list[Any] | None = None) -> None:
        super().__init__(reason)
        self.partial = partial or []


class RateLimiter:
    """Spaces requests at least 1/rate seconds apart. Rate 0 means no limit."""

    def __init__(self, requests_per_second: float, sleep: Sleep = asyncio.sleep) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._next_slot = 0.0
        self._sleep = sleep

    async def acquire(self) -> None:
        # No await between reading and booking the slot, so concurrent callers get different slots.
        now = time.monotonic()
        start = max(now, self._next_slot)
        self._next_slot = start + self._interval
        if start > now:
            await self._sleep(start - now)


@dataclass
class ApiResponse:
    """A successful response: its effective status and the parsed JSON object."""

    status: int
    data: dict[str, Any]


class ApiClient:
    """HTTP client for one external API."""

    def __init__(
        self,
        name: str,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        secrets: Iterable[str | None] = (),
        sleep: Sleep = asyncio.sleep,
        no_retry_statuses: frozenset[int] = frozenset(),
        before_request: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        # Temporary failures that this API must still not be asked about again.
        self._no_retry_statuses = no_retry_statuses
        # Runs before every attempt, retries included; what it raises stops the request.
        self._before_request = before_request
        self._client = client
        self._limiter = limiter or RateLimiter(0)
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._secrets = [secret for secret in secrets if secret]
        self._sleep = sleep

    def mask(self, text: str) -> str:
        """Replace configured secrets in a text received from the outside."""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str] | None = None,
        inspect_body: BodyInspector | None = None,
        ok_statuses: frozenset[int] = frozenset({200}),
        max_attempts: int | None = None,
    ) -> ApiResponse:
        """
        Send a request and return its JSON object, retrying only temporary failures.

        `max_attempts` overrides the default for callers that have their own fallback.
        """
        attempts = max_attempts if max_attempts is not None else self._max_attempts
        host = httpx.URL(url).host
        for attempt in range(1, attempts + 1):
            retry_after: float | None = None
            try:
                status, body, retry_after = await self._send(method, url, params, data)
                payload = self._parse_json(body)
                details = ""
                if payload is not None and inspect_body is not None:
                    body_status, details = inspect_body(payload)
                    status = body_status if body_status is not None else status
                if status in ok_statuses:
                    if payload is None:
                        raise ProviderResponseError(f"{self.name}: ответ не является JSON-объектом")
                    logger.debug("%s %s %s -> %d", self.name, method, host, status)
                    return ApiResponse(status=status, data=payload)
                error = self._error_for_status(status, details)
                retryable = status in _RETRYABLE_STATUSES and status not in self._no_retry_statuses
            except httpx.HTTPError as exc:
                # Only the class name: messages of some httpx errors may contain the URL with the key.
                error = ProviderTemporaryError(f"{self.name}: сетевая ошибка ({type(exc).__name__})")
                retryable = True

            if not retryable or attempt == attempts:
                raise error

            delay = min(retry_after or BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), MAX_RETRY_DELAY_SECONDS)
            logger.warning(
                "%s: попытка %d/%d не удалась (%s), повтор через %.1f с", self.name, attempt, attempts, error, delay
            )
            await self._sleep(delay)
        raise ProviderTemporaryError(f"{self.name}: запрос не выполнен")  # attempts < 1

    async def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str | int] | None,
        data: Mapping[str, str] | None,
    ) -> tuple[int, bytes, float | None]:
        if self._before_request is not None:
            await self._before_request()
        await self._limiter.acquire()
        if self._client is not None:
            return await self._read(self._client, method, url, params, data)
        async with httpx.AsyncClient(verify=_ssl_context()) as client:
            return await self._read(client, method, url, params, data)

    async def _read(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: Mapping[str, str | int] | None,
        data: Mapping[str, str] | None,
    ) -> tuple[int, bytes, float | None]:
        async with client.stream(method, url, params=params, data=data, timeout=self._timeout) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise ProviderResponseError(
                        f"{self.name}: ответ больше {self._max_response_bytes // (1024 * 1024)} МБ"
                    )
                chunks.append(chunk)
            return response.status_code, b"".join(chunks), _parse_retry_after(response)

    @staticmethod
    def _parse_json(body: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _error_for_status(self, status: int, details: str) -> ProviderError:
        suffix = f" {self.mask(details)}" if details else ""
        text = f"{self.name}: код {status}{suffix}"
        if status in _AUTH_STATUSES:
            return ProviderAuthError(f"{text}. Проверьте ключ API и его права")
        if status in _QUOTA_STATUSES:
            return ProviderQuotaError(f"{text}. Превышен лимит запросов или исчерпана квота")
        if status in _RETRYABLE_STATUSES:
            return ProviderTemporaryError(text)
        return ProviderResponseError(text)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Delay from the Retry-After header in seconds; the HTTP-date form is treated as absent."""
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None
