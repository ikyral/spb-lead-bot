"""What a page showed: everything the scraper needs, independent of browser implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PageSnapshot:
    """What a page showed: status, final URL, title, html, json responses, and state."""

    status: int | None
    final_url: str
    title: str
    html: str
    json_bodies: list[dict[str, Any]] = field(default_factory=list)
    # Where each of json_bodies came from, in the same order.
    json_urls: list[str] = field(default_factory=list)
    # Page state dictionary if available.
    page_state: dict[str, Any] = field(default_factory=dict)
    page_state_problems: dict[str, str] = field(default_factory=dict)

    def bodies_from(self, wanted: Callable[[str], bool]) -> list[dict[str, Any]]:
        """The bodies that came from the addresses of interest."""
        return [body for url, body in zip(self.json_urls, self.json_bodies, strict=True) if wanted(url)]

    def bodies_from_or_all(self, wanted: Callable[[str], bool]) -> list[dict[str, Any]]:
        """The same, for a page whose addresses are not known: then only the wanted ones were read at all."""
        return self.bodies_from(wanted) if self.json_urls else self.json_bodies
