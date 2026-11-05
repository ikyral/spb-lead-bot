"""Tests for the shared API client: retries, error classes, limits, no secrets in errors."""

import asyncio
import logging

import httpx
import pytest

from src.services.api_client import (
    ApiClient,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderResponseError,
    ProviderTemporaryError,
    RateLimiter,
)

URL = "https://api.example.test/items"
API_KEY = "SECRET-KEY"


class _Sleeps:
    """Records requested delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _api(handler, sleeps: _Sleeps | None = None, **kwargs) -> ApiClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ApiClient("Test", client=client, secrets=[API_KEY], sleep=sleeps or _Sleeps(), **kwargs)


def _sequence(*responses):
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return handler, calls


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_temporary_status_is_retried_with_exponential_backoff(status: int) -> None:
    handler, calls = _sequence(httpx.Response(status), httpx.Response(status), httpx.Response(200, json={"ok": True}))
    sleeps = _Sleeps()
    response = await _api(handler, sleeps).request_json("GET", URL)
    assert response.data == {"ok": True}
    assert len(calls) == 3
    assert sleeps.delays == [1.0, 2.0]


async def test_network_error_is_retried() -> None:
    handler, calls = _sequence(httpx.ConnectError("boom"), httpx.Response(200, json={}))
    await _api(handler).request_json("GET", URL)
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_invalid_key_is_not_retried(status: int) -> None:
    handler, calls = _sequence(httpx.Response(status))
    sleeps = _Sleeps()
    with pytest.raises(ProviderAuthError, match="ключ"):
        await _api(handler, sleeps).request_json("GET", URL)
    assert len(calls) == 1
    assert sleeps.delays == []


async def test_exhausted_429_is_reported_as_quota_and_attempts_are_bounded() -> None:
    handler, calls = _sequence(*[httpx.Response(429) for _ in range(3)])
    with pytest.raises(ProviderQuotaError, match="квота"):
        await _api(handler).request_json("GET", URL)
    assert len(calls) == 3


async def test_exhausted_5xx_is_temporary() -> None:
    handler, _calls = _sequence(*[httpx.Response(500) for _ in range(3)])
    with pytest.raises(ProviderTemporaryError):
        await _api(handler).request_json("GET", URL)


async def test_retry_after_header_is_respected_but_capped() -> None:
    handler, _calls = _sequence(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "99999"}),
        httpx.Response(200, json={}),
    )
    sleeps = _Sleeps()
    await _api(handler, sleeps).request_json("GET", URL)
    assert sleeps.delays == [7.0, 30.0]


async def test_other_client_errors_are_not_retried() -> None:
    handler, calls = _sequence(httpx.Response(400, json={}))
    with pytest.raises(ProviderResponseError):
        await _api(handler).request_json("GET", URL)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"[1, 2, 3]", b""])
async def test_success_with_unusable_body_is_an_error_not_a_crash(body: bytes) -> None:
    handler, _calls = _sequence(httpx.Response(200, content=body))
    with pytest.raises(ProviderResponseError):
        await _api(handler).request_json("GET", URL)


async def test_oversized_response_is_rejected() -> None:
    handler, _calls = _sequence(httpx.Response(200, content=b"x" * 2048))
    with pytest.raises(ProviderResponseError):
        await _api(handler, max_response_bytes=1024).request_json("GET", URL)


async def test_status_inside_the_body_counts_like_an_http_status() -> None:
    """2GIS answers errors with HTTP 200 and meta.code in the body."""

    def inspect(payload: dict) -> tuple[int | None, str]:
        return payload.get("code"), payload.get("message", "")

    handler, calls = _sequence(
        httpx.Response(200, json={"code": 500}),
        httpx.Response(200, json={"code": 403, "message": "blocked"}),
    )
    with pytest.raises(ProviderAuthError, match="blocked"):
        await _api(handler).request_json("GET", URL, inspect_body=inspect)
    assert len(calls) == 2


async def test_secret_never_reaches_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    leaking_url = f"{URL}?key={API_KEY}"

    handler, _calls = _sequence(
        httpx.ConnectError(f"failed to connect to {leaking_url}"),
        httpx.ConnectError(f"failed to connect to {leaking_url}"),
        httpx.ConnectError(f"failed to connect to {leaking_url}"),
    )
    with pytest.raises(ProviderTemporaryError) as network_error:
        await _api(handler).request_json("GET", URL, params={"key": API_KEY})
    assert API_KEY not in str(network_error.value)

    def inspect(payload: dict) -> tuple[int | None, str]:
        return 403, f"Key {API_KEY} is blocked"

    handler, _calls = _sequence(httpx.Response(200, json={}))
    with pytest.raises(ProviderAuthError) as auth_error:
        await _api(handler).request_json("GET", URL, params={"key": API_KEY}, inspect_body=inspect)
    assert API_KEY not in str(auth_error.value)
    assert API_KEY not in caplog.text


async def test_rate_limiter_spaces_concurrent_requests() -> None:
    sleeps = _Sleeps()
    limiter = RateLimiter(requests_per_second=10, sleep=sleeps)
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    assert len(sleeps.delays) == 2
    assert sleeps.delays[0] == pytest.approx(0.1, abs=0.02)
    assert sleeps.delays[1] == pytest.approx(0.2, abs=0.02)


async def test_rate_limiter_with_zero_rate_never_waits() -> None:
    sleeps = _Sleeps()
    limiter = RateLimiter(0, sleep=sleeps)
    await limiter.acquire()
    await limiter.acquire()
    assert sleeps.delays == []
