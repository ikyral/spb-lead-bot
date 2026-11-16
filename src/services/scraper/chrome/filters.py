"""URL patterns of 2GIS pages and responses."""

from __future__ import annotations

ITEM_RESPONSE_PATTERN: str = r"https://catalog\.api\.2gis\.[^/]+/.*/items/byid"
SEARCH_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/search/.*"
IN_BUILDING_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/inside/.*"
FIRM_URL_PATTERN: str = r"https?://2gis\.[^/]+(/[^/]+)?/firm/.*"
