"""Lead scoring service based on website status, contacts, and business niche."""

import re

from src.services.site_status import SITE_FOUND, SITE_UNKNOWN

STATUS_POINTS: dict[str, int] = {
    "dead_site": 45,
    "social_only": 35,
    "no_site": 25,
    # Contacts are in the source's card: a real lead, ranked below those that can be contacted right away.
    SITE_UNKNOWN: 30,
}

HIGH_MARGIN_KEYWORDS: tuple[str, ...] = (
    "стоматолог",
    "клиник",
    "автосервис",
    "детейлинг",
    "салон красоты",
    "косметолог",
    "юрист",
    "бухучет",
    "отель",
    "база отдыха",
    "мебель",
    "ремонт",
)

HIGH_MARGIN_CATEGORY_KEYS: frozenset[str] = frozenset({
    "dentist_clinic",
    "auto_repair",
    "beauty",
    "legal_b2b",
    "hotels",
})

# 'ремонт' targets renovation and car repair; small repair shops (shoes, clothes, phones,
# watches, appliances) are low-margin. Such phrases are removed before keyword matching,
# so 'Автосервис, ремонт телефонов' still counts as high-margin via 'автосервис'.
LOW_MARGIN_REPAIR_RE = re.compile(
    r"ремонт\w*(?:\s+и\s+\w+)?\s+"
    r"(?:обув\w*|одежд\w*|телефон\w*|смартфон\w*|планшет\w*|ноутбук\w*|компьютер\w*"
    r"|часов|часы|сумок|бытов\w*|техник\w*)"
)


def is_high_margin_category(category: str | None) -> bool:
    """Return True if a free-text category (e.g. from CSV) names a high-margin niche."""
    normalized = LOW_MARGIN_REPAIR_RE.sub(" ", (category or "").lower().replace("ё", "е"))
    return any(keyword in normalized for keyword in HIGH_MARGIN_KEYWORDS)


def calculate_lead_score(
    category_key: str,
    site_status: str,
    phone: str | None,
    socials: str | None,
    high_margin: bool = False,
) -> int:
    """
    Calculate lead score (0..100) based on website status, contacts, and category.

    - Returns 0 if a working site is known ('has_site', 'site_found').
    - Base points: dead_site (+45), social_only (+35), unknown (+30), no_site (+25).
    - Contacts: phone (+25), socials (+15), neither phone nor socials (-30, except 'unknown':
      the source did not report contacts, which says nothing about the organization).
    - Margin bonus: high_margin is True or matches high margin keywords (+15).
    - Final result clamped to [0, 100].
    """
    if site_status in ("has_site", SITE_FOUND):
        return 0

    score = STATUS_POINTS.get(site_status, 0)

    has_phone = bool(phone and phone.strip())
    has_socials = bool(socials and socials.strip())

    if has_phone:
        score += 25
    if has_socials:
        score += 15
    if not has_phone and not has_socials and site_status != SITE_UNKNOWN:
        score -= 30

    is_high = (
        high_margin
        or category_key in HIGH_MARGIN_CATEGORY_KEYS
        or is_high_margin_category(category_key)
    )
    if is_high:
        score += 15

    return max(0, min(100, score))
