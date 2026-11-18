"""URL patterns of 2GIS pages and responses."""

from __future__ import annotations

ITEM_RESPONSE_PATTERN: str = r"https://catalog\.api\.2gis\.[^/]+/.*/items/byid"
SEARCH_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/search/.*"
IN_BUILDING_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/inside/.*"
FIRM_URL_PATTERN: str = r"https?://2gis\.[^/]+(/[^/]+)?/firm/.*"

# What only draws the page: fonts, map tiles and styles, photos and video. A card is read from the answer of
# the catalog and from the state of the page, so none of it is needed, and it is most of the memory of the tab.
# The site's own logs, metrics and ads are NOT here: the site gets to see its usual requests.
HEAVY_RESOURCE_PATTERNS: tuple[str, ...] = (
    "https://d-assets.2gis.*/fonts/*",
    "https://mapgl.2gis.*/api/fonts/*",
    "https://tile*.maps.2gis.*",
    "https://s*.bss.2gis.*",
    "https://styles.api.2gis.*",
    "https://video-pr.api.2gis.*",
    "https://api.photo.2gis.*/*",
    "https://market-backend.api.2gis.*",
    "https://traffic*.edromaps.2gis.*",
    "https://disk.2gis.*/styles/*",
)
