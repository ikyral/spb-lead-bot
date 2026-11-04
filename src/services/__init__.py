"""Services package containing site checker and lead scorer."""

from typing import Any

from src.services.checker import (
    DEFAULT_HEADERS,
    DEFAULT_USER_AGENT,
    SOCIAL_DOMAINS,
    analyze_website,
    is_social_domain,
)
from src.services.scorer import (
    HIGH_MARGIN_CATEGORY_KEYS,
    HIGH_MARGIN_KEYWORDS,
    STATUS_POINTS,
    calculate_lead_score,
)

__all__ = [
    "DEFAULT_HEADERS",
    "DEFAULT_USER_AGENT",
    "HIGH_MARGIN_CATEGORY_KEYS",
    "HIGH_MARGIN_KEYWORDS",
    "SOCIAL_DOMAINS",
    "STATUS_POINTS",
    "analyze_website",
    "calculate_lead_score",
    "import_csv_harvest",
    "is_social_domain",
    "run_lead_harvest",
]


def __getattr__(name: str) -> Any:
    """
    Lazy access to pipeline functions (PEP 562).

    Importing the pipeline pulls in the database layer and creates the SQLAlchemy engine.
    Lightweight modules (formatters, keyboards) import src.services.providers.*, which runs
    this package __init__, so the pipeline must not be imported eagerly here.
    """
    if name in ("import_csv_harvest", "run_lead_harvest"):
        from src.services import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
