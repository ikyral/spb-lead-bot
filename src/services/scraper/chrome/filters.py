"""2GIS request filters, injected XHR tracker, and URL patterns from parser-2gis-new."""

from __future__ import annotations

ITEM_RESPONSE_PATTERN: str = r"https://catalog\.api\.2gis\.[^/]+/.*/items/byid"
SEARCH_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/search/.*"
IN_BUILDING_URL_PATTERN: str = r"https?://2gis\.[^/]+/[^/]+/inside/.*"
FIRM_URL_PATTERN: str = r"https?://2gis\.[^/]+(/[^/]+)?/firm/.*"

# Tracks active XMLHttpRequest calls to 2GIS endpoints on window.openHTTPs.
XHR_COUNTER_SCRIPT: str = r"""
(function() {
    window.openHTTPs = 0;
    var oldOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url, async, user, pass) {
        if (typeof url === "string" && url.match(/^https?\:\/\/[^\/]*2gis\.[a-z]+/i)) {
            if (window.openHTTPs === undefined) {
                window.openHTTPs = 1;
            } else {
                window.openHTTPs++;
            }
            this.addEventListener("readystatechange", function() {
                if (this.readyState === 4) {
                    window.openHTTPs = Math.max(0, (window.openHTTPs || 1) - 1);
                }
            }, false);
        }
        return oldOpen.call(this, method, url, async, user, pass);
    };
})();
"""

_CORE_BLOCKED_URLS: tuple[str, ...] = (
    "https://favorites.api.2gis.*/*",
    "https://2gis.*/_/log",
    "https://2gis.*/_/metrics",
    "https://google-analytics.com/*",
    "https://www.google-analytics.com/*",
    "https://counter.yadro.ru/*",
    "https://www.tns-counter.ru/*",
    "https://mc.yandex.ru/*",
    "https://catalog.api.2gis.ru/3.0/ads/*",
    "https://d-assets.2gis.*/privacyPolicyBanner*.js",
    "https://vk.com/*",
)

_EXTRA_BLOCKED_URLS: tuple[str, ...] = (
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


def blocked_requests(extended: bool = False) -> list[str]:
    """Return URL patterns to block telemetry/ads and optionally heavy media resources."""
    urls = list(_CORE_BLOCKED_URLS)
    if extended:
        urls.extend(_EXTRA_BLOCKED_URLS)
    return urls
