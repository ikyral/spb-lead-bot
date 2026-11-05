"""Normalization of contact data from automatic collection (phones, websites, addresses, emails)."""

import re
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.services.providers.base import RawPlace

_PHONE_SEPARATORS_RE = re.compile(r"[,;]")
_NON_DIGITS_RE = re.compile(r"\D")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Advertising parameters of a link: they identify a campaign, not a page.
_TRACKING_PARAMS: frozenset[str] = frozenset({"gclid", "yclid", "ymclid", "fbclid", "_openstat", "from"})
_E164_MAX_DIGITS = 15
_E164_MIN_DIGITS = 8
_RUSSIAN_DIGITS = 11
_LOCAL_DIGITS = 10
_MANUAL_PHONE_RE = re.compile(r"\+?[\d\s().\-]+", re.ASCII)


def normalize_phone(raw: str | None) -> str | None:
    """
    Convert one phone number to E.164, or return None if that is impossible without guessing.

    Numbers without a country code are treated as Russian. Extensions are dropped. Local
    numbers without a city code ('555-01-01') cannot be converted.
    """
    text = (raw or "").strip()
    digits = _NON_DIGITS_RE.sub("", text)
    if text.startswith("+"):
        if digits.startswith("7") and len(digits) >= 11:
            return f"+{digits[:11]}"
        return f"+{digits}" if _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS else None
    if len(digits) >= 11 and digits[0] in "78":
        return f"+7{digits[1:11]}"
    return f"+7{digits}" if len(digits) == 10 else None


def parse_manual_phone(raw: str | None) -> str | None:
    """
    A number typed by a person, in E.164, or None if the text is not exactly one phone number.

    Stricter than normalize_phone: that one cleans up data from sources and drops extra digits,
    while here extra digits or words mean a typo the person should see.
    """
    text = (raw or "").strip()
    if not _MANUAL_PHONE_RE.fullmatch(text):
        return None
    digits = _NON_DIGITS_RE.sub("", text)
    if text.startswith("+"):
        if digits.startswith("7") and len(digits) != _RUSSIAN_DIGITS:
            return None
    elif len(digits) != _LOCAL_DIGITS and not (len(digits) == _RUSSIAN_DIGITS and digits[0] in "78"):
        return None
    return normalize_phone(text)


def normalize_phone_list(raw: str | None) -> str | None:
    """Normalize a comma-separated list of phones; a value that cannot be converted is kept as is."""
    phones: list[str] = []
    for part in _PHONE_SEPARATORS_RE.split(raw or ""):
        if not part.strip():
            continue
        phone = normalize_phone(part) or part.strip()
        if phone not in phones:
            phones.append(phone)
    return ", ".join(phones) or None


def normalize_website(raw: str | None) -> str | None:
    """Lowercase the host, drop utm/ad parameters, the fragment and a bare trailing slash."""
    text = (raw or "").strip()
    if not text:
        return None
    has_scheme = "://" in text
    try:
        parts = urlsplit(text if has_scheme else f"//{text}")
        host = parts.netloc.lower()
    except ValueError:
        return text
    if not host or parts.scheme not in ("", "http", "https"):
        return text
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = "" if parts.path == "/" and not query else parts.path
    normalized = urlunsplit((parts.scheme, host, path, urlencode(query), ""))
    # urlunsplit keeps the leading '//' of a scheme-less address.
    return normalized if has_scheme else normalized.removeprefix("//")


def normalize_address(raw: str | None) -> str | None:
    """Collapse whitespace and trim stray separators; the case stays as the source wrote it."""
    text = " ".join((raw or "").split()).strip(" ,;")
    return text or None


def normalize_email(raw: str | None) -> str | None:
    """Lowercase a valid email, return None for anything else."""
    text = (raw or "").strip().lower().removeprefix("mailto:")
    return text if _EMAIL_RE.fullmatch(text) else None


def normalize_place(place: RawPlace) -> RawPlace:
    """Return a copy of the place with normalized contact fields."""
    return replace(
        place,
        name=" ".join(place.name.split()),
        phone=normalize_phone_list(place.phone),
        raw_website=normalize_website(place.raw_website),
        address=normalize_address(place.address),
        email=normalize_email(place.email),
    )
