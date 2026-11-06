"""Lead scoring service based on website status, contacts, and business niche."""

import re

from src.services.site_status import (
    SITE_DEAD,
    SITE_FOUND,
    SITE_HAS_SITE,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNCERTAIN,
    SITE_UNKNOWN,
    UNVERIFIED_STATUSES,
)

# All the weights are here. The more certain it is that a site is needed, the higher.
STATUS_POINTS: dict[str, int] = {
    SITE_DEAD: 60,
    SITE_SOCIAL_ONLY: 55,
    SITE_NO_SITE: 50,
    SITE_NOT_FOUND: 40,
    SITE_UNCERTAIN: 35,
    SITE_UNKNOWN: 30,
}
MOBILE_PHONE_POINTS: int = 25
OTHER_PHONE_POINTS: int = 15
SOCIALS_POINTS: int = 10
NO_CONTACTS_POINTS: int = -35
HIGH_MARGIN_POINTS: int = 10
LOW_PRIORITY_POINTS: int = -10
# Two branches: not yet a chain to leave out, but less likely to be a small business.
SMALL_CHAIN_POINTS: int = -10
# A lead with a verdict about its site is kept only from this score.
MIN_LEAD_SCORE: int = 30

# Collected, but a site is rarely what they lack: large places have their own, cafes live on aggregators.
LOW_PRIORITY_CATEGORY_KEYS: frozenset[str] = frozenset({"hotels", "horeca"})

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

HIGH_MARGIN_CATEGORY_KEYS: frozenset[str] = frozenset(
    {
        "dentist_clinic",
        "auto_repair",
        "beauty",
        "legal_b2b",
        "massage",
        "detailing",
        "cleaning",
        "renovation",
    }
)

# 'ремонт' targets renovation and car repair; small repair shops (shoes, clothes, phones,
# watches, appliances) are low-margin. Such phrases are removed before keyword matching,
# so 'Автосервис, ремонт телефонов' still counts as high-margin via 'автосервис'.
LOW_MARGIN_REPAIR_RE = re.compile(
    r"ремонт\w*(?:\s+и\s+\w+)?\s+"
    r"(?:обув\w*|одежд\w*|телефон\w*|смартфон\w*|планшет\w*|ноутбук\w*|компьютер\w*"
    r"|часов|часы|сумок|бытов\w*|техник\w*)"
)

_PHONE_SEPARATORS_RE = re.compile(r"[,;]")
_NON_DIGITS_RE = re.compile(r"\D")


def is_high_margin_category(category: str | None) -> bool:
    """Return True if a free-text category (e.g. from CSV) names a high-margin niche."""
    normalized = LOW_MARGIN_REPAIR_RE.sub(" ", (category or "").lower().replace("ё", "е"))
    return any(keyword in normalized for keyword in HIGH_MARGIN_KEYWORDS)


def has_mobile_phone(phone: str | None) -> bool:
    """Is any of the numbers a Russian mobile one? A message to it is the easiest way to reach the owner."""
    for part in _PHONE_SEPARATORS_RE.split(phone or ""):
        digits = _NON_DIGITS_RE.sub("", part)
        if (len(digits) == 11 and digits[0] in "78" and digits[1] == "9") or (len(digits) == 10 and digits[0] == "9"):
            return True
    return False


def calculate_lead_score(
    category_key: str,
    site_status: str,
    phone: str | None,
    socials: str | None,
    high_margin: bool = False,
    small_chain: bool = False,
) -> int:
    """
    Calculate lead score (0..100) based on website status, contacts, and category: see the weights above.

    Returns 0 if a working site is known. A lack of contacts lowers the score, except where nothing
    is verified: then the source did not report contacts, which says nothing about the organization.
    The final result is clamped to [0, 100].
    """
    if site_status in (SITE_HAS_SITE, SITE_FOUND):
        return 0

    score = STATUS_POINTS.get(site_status, 0)

    has_phone = bool(phone and phone.strip())
    has_socials = bool(socials and socials.strip())

    if has_phone:
        score += MOBILE_PHONE_POINTS if has_mobile_phone(phone) else OTHER_PHONE_POINTS
    if has_socials:
        score += SOCIALS_POINTS
    if not has_phone and not has_socials and site_status not in UNVERIFIED_STATUSES:
        score += NO_CONTACTS_POINTS

    is_high = high_margin or category_key in HIGH_MARGIN_CATEGORY_KEYS or is_high_margin_category(category_key)
    if is_high:
        score += HIGH_MARGIN_POINTS
    if category_key in LOW_PRIORITY_CATEGORY_KEYS:
        score += LOW_PRIORITY_POINTS
    if small_chain:
        score += SMALL_CHAIN_POINTS

    return max(0, min(100, score))


def is_worth_saving(site_status: str, score: int) -> bool:
    """
    Should a place become a lead?

    Nothing is verified about the site: the score only says how soon to look at it, so
    such leads stay whatever it is. Otherwise they would disappear with a demo key of 2GIS.
    """
    if site_status in (SITE_HAS_SITE, SITE_FOUND):
        return False
    return site_status in UNVERIFIED_STATUSES or score >= MIN_LEAD_SCORE
