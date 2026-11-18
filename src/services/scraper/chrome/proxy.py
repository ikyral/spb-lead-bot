"""
Proxy for Chrome: parsing of the URL, a log-safe form of it and the check of the country of the exit IP.

Chrome's --proxy-server accepts only scheme://host:port and silently ignores a login and a password. So
the URL is split: the server part goes to the command line, the credentials are given over CDP when the
proxy asks for them (Fetch.authRequired, see remote.py).

Accepted forms:
    http://host:port
    http://user:password@host:port      (special characters of the password in %XX form, or a plain @ in it)
    https://user:password@host:port
    socks5://host:port                  (no login: Chrome cannot authenticate against SOCKS)
    host:port                           (taken as http://)
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field

_SUPPORTED_SCHEMES: tuple[str, ...] = ("http", "https", "socks4", "socks5")
COUNTRY_CHECK_OFF: str = "OFF"
_COUNTRY_OFF_WORDS: frozenset[str] = frozenset({"OFF", "NO", "ANY", "0"})

# Services that answer with the IP and the country of the caller as JSON: (url, country field, ip field).
# They are opened in the browser itself, so the check sees exactly the exit IP that 2GIS will see.
IP_CHECK_SERVICES: tuple[tuple[str, str, str], ...] = (
    ("https://ipinfo.io/json", "country", "ip"),
    ("https://ipapi.co/json/", "country_code", "ip"),
    ("https://api.country.is/", "country", "ip"),
)


@dataclass(frozen=True)
class ProxySettings:
    """A parsed proxy. The password is left out of repr(), so it cannot reach a log by accident."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    @property
    def server(self) -> str:
        """The value of --proxy-server: no credentials."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def has_auth(self) -> bool:
        return bool(self.username)

    def masked(self) -> str:
        """The proxy for logs and messages: the password is replaced, the login is shown."""
        auth = f"{self.username}:***@" if self.username else ""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{auth}{host}:{self.port}"


def parse_proxy(proxy: str) -> ProxySettings:
    """
    Parse and check a proxy URL.

    Raises ValueError with a message that never repeats the URL: it is made for the owner and may be logged.
    """
    text = proxy.strip()
    if "://" not in text:
        text = f"http://{text}"
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("прокси должен быть в формате схема://[логин:пароль@]хост:порт") from None

    scheme = parsed.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"  # Chrome resolves names through SOCKS5 anyway
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(
            f"неподдерживаемая схема прокси {parsed.scheme!r} (допустимо: {', '.join(_SUPPORTED_SCHEMES)})"
        )
    if not hostname or not port:
        raise ValueError("прокси должен быть в формате схема://[логин:пароль@]хост:порт")

    username = urllib.parse.unquote(parsed.username) if parsed.username else None
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    if username and scheme.startswith("socks"):
        raise ValueError(
            "Chrome не поддерживает SOCKS-прокси с логином и паролем: используйте HTTP(S)-прокси "
            "или локальный переходник без авторизации"
        )
    return ProxySettings(scheme=scheme, host=hostname, port=port, username=username, password=password or "")


def normalize_country(value: str) -> str:
    """'ru' -> 'RU'; 'off', 'no', 'any', '0' -> 'OFF' (no check). Raises ValueError for anything else."""
    text = value.strip().upper()
    if text in _COUNTRY_OFF_WORDS:
        return COUNTRY_CHECK_OFF
    if len(text) != 2 or not text.isalpha():
        raise ValueError("код страны: две буквы, например RU, или off, чтобы отключить проверку")
    return text


def extract_ip_info(body: str, country_field: str, ip_field: str) -> tuple[str | None, str | None]:
    """The (ip, country code) from the JSON answer of an IP service; (None, None) if it is not that."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    country = data.get(country_field)
    ip = data.get(ip_field)
    return (str(ip) if ip else None, str(country).upper() if country else None)
