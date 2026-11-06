"""
robots.txt rules (RFC 9309).

urllib.robotparser is not used: it does not understand '*' and '$' in paths, and real files
rely on them (2gis.ru: 'Disallow: */user*', 'Disallow: /*stat=').
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

ROBOTS_TIMEOUT_SECONDS: float = 15.0
# Pauses between attempts when the site cannot be reached at all.
ROBOTS_RETRY_PAUSES_SECONDS: tuple[float, ...] = (5.0, 15.0)


class RobotsUnavailableError(Exception):
    """robots.txt cannot be read, so it is unknown what is allowed."""


class RobotsNoConnectionError(RobotsUnavailableError):
    """Not a single answer from the site, whatever the number of attempts."""


@dataclass
class RobotsRules:
    """Rules of one user-agent group. Without rules everything is allowed."""

    # (is_allowed, pattern length, compiled pattern)
    rules: list[tuple[bool, int, re.Pattern[str]]] = field(default_factory=list)

    def is_allowed(self, url: str) -> bool:
        """Check a full URL: the longest matching rule wins, 'Allow' wins a tie."""
        target = httpx.URL(url).raw_path.decode("ascii")
        matches = [(length, allowed) for allowed, length, pattern in self.rules if pattern.match(target)]
        if not matches:
            return True
        return max(matches)[1]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def _encoded(pattern: str) -> str:
    """Files may contain raw Cyrillic; URLs are compared percent-encoded."""
    return quote(pattern, safe="/*$?=&%:@!,;+-._~'()")


def parse_robots(text: str, agent: str) -> RobotsRules:
    """Parse the group for `agent`, or the '*' group if there is no group for it."""
    groups: dict[str, list[tuple[bool, str]]] = {}
    current: list[str] = []
    in_rules = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        name, separator, value = line.partition(":")
        if not separator:
            continue
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            if in_rules:
                current, in_rules = [], False
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif name in ("allow", "disallow") and current:
            in_rules = True
            if value:  # an empty Disallow allows everything and adds no rule
                for group in current:
                    groups[group].append((name == "allow", value))

    selected = groups.get(agent.lower(), groups.get("*", []))
    rules = RobotsRules()
    for allowed, value in selected:
        encoded = _encoded(value)
        rules.rules.append((allowed, len(encoded), _compile(encoded)))
    return rules


async def _get_robots(url: str, client: httpx.AsyncClient | None) -> httpx.Response:
    if client is not None:
        return await client.get(url, timeout=ROBOTS_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(follow_redirects=True) as own_client:
        return await own_client.get(url, timeout=ROBOTS_TIMEOUT_SECONDS)


async def fetch_robots(
    base_url: str,
    agent: str,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RobotsRules:
    """
    Load the rules for a site, following RFC 9309.

    A missing file (4xx) means no restrictions. If the file cannot be read (5xx, network),
    everything counts as forbidden: RobotsUnavailableError is raised. Only a network failure is
    tried again: an answer of the site, whatever it is, is its answer.
    """
    url = f"{base_url}/robots.txt"
    pauses = iter(ROBOTS_RETRY_PAUSES_SECONDS)
    while True:
        try:
            response = await _get_robots(url, client)
            break
        except httpx.HTTPError as exc:
            pause = next(pauses, None)
            if pause is None:
                raise RobotsNoConnectionError(f"сетевая ошибка ({type(exc).__name__})") from None
            await sleep(pause)
    if response.status_code == 200:
        return parse_robots(response.text, agent)
    if 400 <= response.status_code < 500:
        return RobotsRules()
    raise RobotsUnavailableError(f"код {response.status_code}")
