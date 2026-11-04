"""
SSRF protection for website checks.

Website URLs come from third parties (OSM, 2GIS, imported CSV), so the checker must not
reach loopback, private, link-local (cloud metadata 169.254.169.254) or other non-public
addresses, neither directly nor via redirects or DNS names pointing inside the network.

The check happens at the TCP connect level for every connection (including each redirect
hop): the host is resolved once, all addresses are validated, and the connection is made
to a validated IP. TLS still uses the original hostname for SNI and certificate checks
(httpcore takes it from the request origin), so DNS rebinding between check and connect
is not possible.
"""

import asyncio
import ipaddress
import logging
import socket
import typing
from collections.abc import Awaitable, Callable

import httpcore
import httpx

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], Awaitable[list[str]]]


def is_public_ip(address: str) -> bool:
    """Return True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class PublicOnlyNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that refuses to connect to non-public addresses."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolver = resolver or _system_resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if _is_ip_literal(host):
            addresses = [host]
        else:
            try:
                addresses = await asyncio.wait_for(self._resolver(host, port), timeout)
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout(f"DNS resolution timed out for {host}") from exc
            except OSError as exc:
                raise httpcore.ConnectError(f"DNS resolution failed for {host}: {exc}") from exc

        if not addresses:
            raise httpcore.ConnectError(f"No addresses found for {host}")

        blocked = [address for address in addresses if not is_public_ip(address)]
        if blocked:
            logger.warning("Blocked connection to non-public address: %s -> %s", host, blocked[0])
            raise httpcore.ConnectError(f"Blocked connection to non-public address {host} -> {blocked[0]}")

        # IPv4 first: containers often have no IPv6 route. Fall through on connect errors.
        ordered = sorted(addresses, key=lambda address: ipaddress.ip_address(address).version)
        last_error: httpcore.ConnectError | None = None
        for address in ordered:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError(f"Could not connect to {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix socket connections are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connections go through PublicOnlyNetworkBackend."""

    def __init__(self, network_backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        # Loading CA certificates is slow (~150 ms): build the context once and share it.
        ssl_context = httpx.create_ssl_context(trust_env=False)
        super().__init__(verify=ssl_context, trust_env=False)
        # httpx does not expose network_backend, so the pool is rebuilt with httpx defaults
        # (Limits: 100 connections, 20 keep-alive, 5s expiry). Covered by tests.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=network_backend or PublicOnlyNetworkBackend(),
        )
