"""Tests for SSRF protection: website checks must never reach non-public addresses."""

import httpcore
import httpx
import pytest

from src.services.checker import analyze_website, create_check_client
from src.services.safe_http import PublicOnlyNetworkBackend, is_public_ip

OK_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
PUBLIC_IP = "93.184.215.14"


def _redirect_to(location: str) -> bytes:
    return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()


class FakeInternet(httpcore.AsyncNetworkBackend):
    """Inner backend: records connections and serves canned responses per connected IP."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        if host not in self.responses:
            raise httpcore.ConnectError(f"unreachable {host}")
        return await httpcore.AsyncMockBackend([self.responses[host]]).connect_tcp(host, port)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets must not be used")

    async def sleep(self, seconds: float) -> None:
        return None


def _fake_dns(table: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError(f"Name or service not known: {host}")
        return table[host]

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "fe80::1%eth0",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert is_public_ip(address) is False


@pytest.mark.parametrize("address", [PUBLIC_IP, "77.88.55.242", "2a02:6b8::2:242", "::ffff:93.184.215.14"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_ip(address) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost:6379/",
        "http://internal.example/",
    ],
)
async def test_check_blocks_non_public_targets_without_connecting(url: str) -> None:
    inner = FakeInternet({})
    backend = PublicOnlyNetworkBackend(
        inner=inner,
        resolver=_fake_dns({"localhost": ["127.0.0.1", "::1"], "internal.example": ["10.0.0.5"]}),
    )
    async with create_check_client(network_backend=backend) as client:
        assert await analyze_website(url, client=client) == ("dead_site", "HTTP/Connection error")
    assert inner.connected == []


@pytest.mark.asyncio
async def test_check_blocks_host_resolving_to_any_private_address() -> None:
    """If DNS returns a mix of public and private addresses, the host is rejected entirely."""
    inner = FakeInternet({PUBLIC_IP: OK_RESPONSE})
    backend = PublicOnlyNetworkBackend(inner=inner, resolver=_fake_dns({"mixed.example": [PUBLIC_IP, "10.0.0.5"]}))
    async with create_check_client(network_backend=backend) as client:
        assert await analyze_website("http://mixed.example/", client=client) == ("dead_site", "HTTP/Connection error")
    assert inner.connected == []


@pytest.mark.asyncio
async def test_check_blocks_redirect_to_private_address() -> None:
    """A public site redirecting to the metadata endpoint must not be followed."""
    inner = FakeInternet({PUBLIC_IP: _redirect_to("http://169.254.169.254/latest/meta-data/")})
    backend = PublicOnlyNetworkBackend(inner=inner, resolver=_fake_dns({"redirector.example": [PUBLIC_IP]}))
    async with create_check_client(network_backend=backend) as client:
        res = await analyze_website("http://redirector.example/", client=client)
    assert res == ("dead_site", "HTTP/Connection error")
    assert inner.connected == [(PUBLIC_IP, 80)]


@pytest.mark.asyncio
async def test_check_connects_to_validated_ip_of_public_host() -> None:
    """Public sites still work; the connection goes to the validated IP (no second DNS lookup)."""
    inner = FakeInternet({PUBLIC_IP: OK_RESPONSE})
    backend = PublicOnlyNetworkBackend(inner=inner, resolver=_fake_dns({"company.example": [PUBLIC_IP]}))
    async with create_check_client(network_backend=backend) as client:
        res = await analyze_website("http://company.example/", client=client)
    assert res == ("has_site", "http://company.example/")
    assert inner.connected == [(PUBLIC_IP, 80)]


@pytest.mark.asyncio
async def test_backend_tries_next_address_and_prefers_ipv4() -> None:
    """Containers often lack IPv6: IPv4 goes first, and a failing address falls through to the next one."""
    inner = FakeInternet({"93.184.215.15": OK_RESPONSE})
    backend = PublicOnlyNetworkBackend(
        inner=inner,
        resolver=_fake_dns({"dual.example": ["2a02:6b8::2:242", PUBLIC_IP, "93.184.215.15"]}),
    )
    await backend.connect_tcp("dual.example", 80)
    assert inner.connected == [(PUBLIC_IP, 80), ("93.184.215.15", 80)]


@pytest.mark.asyncio
async def test_dns_failure_is_a_connect_error() -> None:
    backend = PublicOnlyNetworkBackend(inner=FakeInternet({}), resolver=_fake_dns({}))
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("no-such-host.example", 80)


@pytest.mark.asyncio
async def test_unix_sockets_are_refused() -> None:
    backend = PublicOnlyNetworkBackend(inner=FakeInternet({}), resolver=_fake_dns({}))
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/var/run/docker.sock")


def test_check_client_uses_ssrf_safe_transport_and_ignores_env_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env proxies would bypass the IP check (only the proxy host would be validated)."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    client = create_check_client()
    transport = client._transport_for_url(httpx.URL("https://example.com"))
    pool = transport._pool
    assert isinstance(pool, httpcore.AsyncConnectionPool)
    assert isinstance(pool._network_backend, PublicOnlyNetworkBackend)
