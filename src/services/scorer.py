"""Lead scoring service based on website status, contacts, and business niche."""

STATUS_POINTS: dict[str, int] = {
    "dead_site": 45,
    "social_only": 35,
    "no_site": 25,
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


def calculate_lead_score(
    category_key: str,
    site_status: str,
    phone: str | None,
    socials: str | None,
    high_margin: bool = False,
) -> int:
    """
    Calculate lead score (0..100) based on website status, contacts, and category.

    - Returns 0 if site_status == 'has_site'.
    - Base points: dead_site (+45), social_only (+35), no_site (+25).
    - Contacts: phone (+25), socials (+15), neither phone nor socials (-30).
    - Margin bonus: high_margin is True or matches high margin keywords (+15).
    - Final result clamped to [0, 100].
    """
    if site_status == "has_site":
        return 0

    score = STATUS_POINTS.get(site_status, 0)

    has_phone = bool(phone and phone.strip())
    has_socials = bool(socials and socials.strip())

    if has_phone:
        score += 25
    if has_socials:
        score += 15
    if not has_phone and not has_socials:
        score -= 30

    norm_category = (category_key or "").lower().strip()
    is_high = (
        high_margin
        or category_key in HIGH_MARGIN_CATEGORY_KEYS
        or any(keyword in norm_category for keyword in HIGH_MARGIN_KEYWORDS)
    )
    if is_high:
        score += 15

    return max(0, min(100, score))
