"""
What is known about the website of a lead: the values of Lead.site_status and how far to trust them.

The honest difference matters here: 'no_site' means a source or a person says there is none,
'site_not_found' only that an automatic search found nothing, 'unknown' that nobody has looked.
"""

from urllib.parse import quote_plus

# The source lists no site, or a person said so.
SITE_NO_SITE: str = "no_site"
# A site is listed, but it does not open.
SITE_DEAD: str = "dead_site"
# A social network or a booking page instead of a site.
SITE_SOCIAL_ONLY: str = "social_only"
# Nobody knows: the source reports no contacts at all (a demo key of 2GIS).
SITE_UNKNOWN: str = "unknown"
# A working site of the organization is known: such a lead is never handed out.
SITE_FOUND: str = "site_found"
# There is a candidate, but it may belong to someone else.
SITE_UNCERTAIN: str = "site_uncertain"
# An automatic search found nothing. That is not proof of there being no site.
SITE_NOT_FOUND: str = "site_not_found"
# Only the checker's answer: a place with a working site from its source is not saved at all.
SITE_HAS_SITE: str = "has_site"

CONFIDENCE_HIGH: str = "high"
CONFIDENCE_MEDIUM: str = "medium"
CONFIDENCE_LOW: str = "low"

SOURCE_2GIS: str = "2gis"
SOURCE_OSM: str = "osm"
SOURCE_SEARCH: str = "search"
SOURCE_MANUAL: str = "manual"

# The order of handing out: every later group goes only after the earlier ones.
ISSUE_GROUPS: tuple[tuple[str, ...], ...] = (
    (SITE_NO_SITE, SITE_DEAD, SITE_SOCIAL_ONLY),
    (SITE_NOT_FOUND,),
    (SITE_UNCERTAIN,),
    (SITE_UNKNOWN,),
)
# No verdict about the site yet: such leads are worth keeping whatever their score is.
UNVERIFIED_STATUSES: frozenset[str] = frozenset({SITE_UNKNOWN, SITE_UNCERTAIN, SITE_NOT_FOUND})
# Used in the link only, so an absurdly long card cannot break the button.
_SEARCH_TEXT_LIMIT: int = 200


def issue_group(site_status: str) -> int:
    """The position of a status in the order of handing out."""
    for position, group in enumerate(ISSUE_GROUPS):
        if site_status in group:
            return position
    return len(ISSUE_GROUPS)


def source_of_status(source: str) -> str | None:
    """Which source a status came from, for places whose source is one of those tracked."""
    return source if source in (SOURCE_2GIS, SOURCE_OSM) else None


def confidence_of_check(site_status: str, source: str) -> str | None:
    """
    How far to trust a status from a source or from checking the listed address.

    A listed address was really opened. The absence of a site is weaker: OSM often just lacks the tag.
    """
    if site_status in (SITE_DEAD, SITE_SOCIAL_ONLY):
        return CONFIDENCE_HIGH
    if site_status == SITE_NO_SITE:
        return CONFIDENCE_LOW if source == SOURCE_OSM else CONFIDENCE_MEDIUM
    return None


def yandex_search_url(name: str, address: str | None) -> str:
    """An ordinary link for a person to open: the bot itself never sends anything to a search engine."""
    text = f"{name} {address or 'Санкт-Петербург'}"[:_SEARCH_TEXT_LIMIT]
    return f"https://yandex.ru/search/?text={quote_plus(text)}"
