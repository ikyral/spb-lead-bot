"""Parsing of 2GIS catalog items (the JSON of a card) into RawPlace. Pure functions, no network."""

import logging
import re
from typing import Any

from src.services.checker import is_social_domain, url_host
from src.services.providers.base import RawPlace
from src.services.twogis_links import DEFAULT_CITY_SLUG, build_card_url

logger = logging.getLogger(__name__)


def parse_catalog_items(
    data: dict[str, Any],
    district_name: str | None,
    category_key: str,
    high_margin: bool,
    city_slug: str = DEFAULT_CITY_SLUG,
) -> list[RawPlace]:
    """Safely parse 2GIS catalog items into RawPlace list."""
    places: list[RawPlace] = []
    result = data.get("result")
    items = result.get("items") if isinstance(result, dict) else None

    for item in items if isinstance(items, list) else []:
        # A text search may also return buildings and streets: only organizations are leads.
        if not isinstance(item, dict) or item.get("type") not in (None, "branch"):
            continue
        raw_id = item.get("id")
        raw_name = item.get("name")
        if not raw_id or not raw_name or not str(raw_name).strip():
            continue

        external_id = f"2gis_{raw_id}"
        name = str(raw_name).strip()

        address = None
        addr_data = item.get("address")
        if isinstance(addr_data, dict):
            address = addr_data.get("name")
        elif isinstance(addr_data, str):
            address = addr_data
        if not address:
            address = item.get("address_name")
        address_str = str(address).strip() if address and str(address).strip() else None

        map_url = None
        point = item.get("point")
        if isinstance(point, dict):
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is not None and lon is not None:
                map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"

        phones, websites, socials_list, emails = extract_contacts(item)
        phone_str = ", ".join(phones) if phones else None
        raw_website = websites[0] if websites else None
        socials_str = ", ".join(socials_list) if socials_list else None

        if not socials_str and raw_website and is_social_domain(url_host(raw_website)):
            socials_str = raw_website

        # A catalog item has no field with this link, it is built from the id.
        source_url = build_card_url(city_slug, raw_id)
        if source_url is None:
            logger.warning("2GIS: cannot build a card link for id %r, the lead is saved without it", raw_id)

        places.append(
            RawPlace(
                external_id=external_id,
                source="2gis",
                name=name,
                category=category_key,
                district=district_name,
                address=address_str,
                phone=phone_str,
                socials=socials_str,
                raw_website=raw_website,
                map_url=map_url,
                high_margin=high_margin,
                email=emails[0] if emails else None,
                source_url=source_url,
                rubric=_primary_rubric(item),
                contacts_known=bool(phones or websites or socials_list or emails),
            )
        )

    return places


def _primary_rubric(item: dict[str, Any]) -> str | None:
    """The name of the main rubric of an organization; if none is marked as main, the first one."""
    rubrics = item.get("rubrics")
    named = [
        rubric
        for rubric in (rubrics if isinstance(rubrics, list) else [])
        if isinstance(rubric, dict) and isinstance(rubric.get("name"), str) and rubric["name"].strip()
    ]
    if not named:
        return None
    primary = next((rubric for rubric in named if rubric.get("kind") == "primary"), named[0])
    return str(primary["name"]).strip()[:255]


# A site of an organization in a card of 2GIS may be a link through 2GIS, with the address itself in its text.
_REDIRECT_HOST_RE = re.compile(r"(?:^|\.)2gis\.[a-z]{2,10}$", re.ASCII)
_SOCIAL_CONTACT_TYPES: frozenset[str] = frozenset(
    {
        "vkontakte",
        "vk",
        "telegram",
        "instagram",
        "whatsapp",
        "viber",
        "odnoklassniki",
        "youtube",
        "facebook",
        "twitter",
        "social",
    }
)


def _site_address(contact: dict[str, Any]) -> str | None:
    """The address of the site itself, or None if only a link through 2GIS is known."""
    for key in ("value", "url", "text"):
        candidate = str(contact.get(key) or "").strip()
        host = url_host(candidate) if candidate and " " not in candidate else None
        if host and "." in host and not _REDIRECT_HOST_RE.search(host.lower()):
            return candidate
    return None


def extract_contacts(item: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Extract phones, websites, socials and emails from contact_groups."""
    phones: list[str] = []
    websites: list[str] = []
    socials: list[str] = []
    emails: list[str] = []

    groups = item.get("contact_groups")
    for group in groups if isinstance(groups, list) else []:
        contacts = group.get("contacts") if isinstance(group, dict) else None
        for contact in contacts if isinstance(contacts, list) else []:
            if not isinstance(contact, dict):
                continue
            c_type = str(contact.get("type") or "").lower().strip()
            val = contact.get("value") or contact.get("text") or contact.get("url")
            if not val or not str(val).strip():
                continue
            val_str = str(val).strip()

            if c_type == "phone":
                if val_str not in phones:
                    phones.append(val_str)
            elif c_type == "email":
                if val_str not in emails:
                    emails.append(val_str)
            elif c_type in ("website", "url"):
                address = _site_address(contact)
                if address is None:
                    continue
                target = socials if is_social_domain(url_host(address)) else websites
                if address not in target:
                    target.append(address)
            elif c_type in _SOCIAL_CONTACT_TYPES and val_str not in socials:
                socials.append(val_str)

    return phones, websites, socials, emails
