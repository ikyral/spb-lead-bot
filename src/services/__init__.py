"""Services package containing site checker and lead scorer."""

from src.services.checker import (
    DEFAULT_HEADERS,
    DEFAULT_USER_AGENT,
    SOCIAL_DOMAINS,
    analyze_website,
    is_social_domain,
)
from src.services.pipeline import (
    import_csv_harvest,
    run_lead_harvest,
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
