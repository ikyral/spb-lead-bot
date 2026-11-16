"""Options for the Chrome CDP browser and 2GIS parser."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PositiveInt

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
    """Chrome launch options. The window is always visible: a person sits at the computer."""

    model_config = ConfigDict(validate_assignment=True)

    binary_path: Path | None = None
    profile_dir: Path | None = None
    start_maximized: bool = True
    silent_browser: bool = True
    memory_limit: PositiveInt = default_memory_limit()


class ChromeParserOptions(BaseModel):
    """Behavior options for DOM click/pagination parsing on 2GIS. Pauses are decided by the gate."""

    model_config = ConfigDict(validate_assignment=True)

    skip_404_response: bool = True
    use_gc: bool = False
    gc_pages_interval: PositiveInt = 10
