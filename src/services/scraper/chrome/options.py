"""Options for the Chrome CDP browser and 2GIS parser."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PositiveInt, SecretStr, model_validator

from .proxy import COUNTRY_CHECK_OFF, normalize_country, parse_proxy
from .utils import floor_to_hundreds

_FALLBACK_MEMORY_LIMIT_MB: int = 2000


def default_memory_limit() -> int:
    """Default memory limit for V8: 75% of total RAM rounded down to hundreds of MB."""
    try:
        import psutil  # type: ignore[import-untyped]  # optional, not in requirements

        memory_total_mb = psutil.virtual_memory().total / (1024**2)
    except ImportError:
        if hasattr(os, "sysconf"):
            try:
                pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                memory_total_mb = (pages * page_size) / (1024**2)
            except (OSError, ValueError):
                return _FALLBACK_MEMORY_LIMIT_MB
        else:
            # Windows has no sysconf: a fixed limit is enough for one tab.
            return _FALLBACK_MEMORY_LIMIT_MB
    limit = floor_to_hundreds(round(0.75 * memory_total_mb))
    return max(limit, 100)


class ChromeOptions(BaseModel):
    """
    Chrome launch options.

    The window is visible by default: a person sits at the computer, or the server gives it a virtual screen
    (Xvfb). `headless` is an option for the cases without one.
    """

    # An error of validation must not print the input: it may hold the password of the proxy.
    model_config = ConfigDict(validate_assignment=True, hide_input_in_errors=True)

    binary_path: Path | None = None
    profile_dir: Path | None = None
    start_maximized: bool = True
    silent_browser: bool = True
    memory_limit: PositiveInt = default_memory_limit()
    headless: bool = False
    # The light mode for a small machine: no images, fonts and map tiles, one renderer, no extensions.
    light: bool = False
    # scheme://[login:password@]host:port. SecretStr keeps the password out of repr() and logs.
    proxy: SecretStr | None = None
    # Required country of the exit IP, checked in the browser before the first request to 2GIS; "OFF" = no check.
    proxy_country: str = COUNTRY_CHECK_OFF
    # Timezone and language of the browser: they should fit the place of the proxy.
    timezone: str | None = None
    lang: str | None = None

    @model_validator(mode="after")
    def _check_proxy_and_country(self) -> ChromeOptions:
        if self.proxy is not None:
            parse_proxy(self.proxy.get_secret_value())
        object.__setattr__(self, "proxy_country", normalize_country(self.proxy_country))
        return self

    @property
    def country_to_check(self) -> str | None:
        """The country the exit IP must be from, or None if it is not checked."""
        return None if self.proxy_country == COUNTRY_CHECK_OFF else self.proxy_country


class ChromeParserOptions(BaseModel):
    """Behavior options for DOM click/pagination parsing on 2GIS. Pauses are decided by the gate."""

    model_config = ConfigDict(validate_assignment=True)

    skip_404_response: bool = True
    use_gc: bool = False
    gc_pages_interval: PositiveInt = 10
