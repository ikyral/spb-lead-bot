"""
Contacts of an organization as links: one form for each, where they came from, no duplicates.

Pure functions, no network. A card of 2GIS gives a contact as {type, value, url, text}: a network
may come as a link, as '@nickname' or, for WhatsApp, as a phone number. Each becomes a link the
owner can open: mailto:, https://t.me/..., https://wa.me/..., https://vk.com/... The same link
from 2GIS and from Google is stored once, with both sources.
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from src.services.normalize import normalize_email

SOURCE_2GIS: str = "2gis"
SOURCE_GOOGLE: str = "google"
KNOWN_SOURCES: tuple[str, ...] = (SOURCE_2GIS, SOURCE_GOOGLE)

KIND_EMAIL = "email"
KIND_TELEGRAM = "telegram"
KIND_WHATSAPP = "whatsapp"
KIND_VK = "vk"
KIND_INSTAGRAM = "instagram"
KIND_OTHER = "other"

# Types of contacts in a card of 2GIS, by what the owner would open.
_TYPE_KINDS: dict[str, str] = {
    "email": KIND_EMAIL,
    "telegram": KIND_TELEGRAM,
    "whatsapp": KIND_WHATSAPP,
    "vkontakte": KIND_VK,
    "vk": KIND_VK,
    "instagram": KIND_INSTAGRAM,
}
# Other networks 2GIS names: kept as links when the value is one, whatever the host.
_OTHER_SOCIAL_TYPES: frozenset[str] = frozenset({"viber", "odnoklassniki", "youtube", "facebook", "twitter", "social"})
# Hosts of messengers and networks, and the host each is stored under.
_HOSTS: dict[str, tuple[str, str]] = {
    "t.me": (KIND_TELEGRAM, "t.me"),
    "telegram.me": (KIND_TELEGRAM, "t.me"),
    "wa.me": (KIND_WHATSAPP, "wa.me"),
    "api.whatsapp.com": (KIND_WHATSAPP, "wa.me"),
    "whatsapp.com": (KIND_WHATSAPP, "wa.me"),
    "vk.com": (KIND_VK, "vk.com"),
    "vk.ru": (KIND_VK, "vk.com"),
    "m.vk.com": (KIND_VK, "vk.com"),
    "instagram.com": (KIND_INSTAGRAM, "instagram.com"),
}
# What a card shows as the text of a button: the name of a network, not a nickname.
_NETWORK_NAMES: frozenset[str] = frozenset({"telegram", "instagram", "whatsapp", "vk", "vkontakte", "tg"})
_NICKNAME_RE = re.compile(r"@?([A-Za-z0-9_.]{3,64})")
_MAX_LINKS: int = 20
_LINK_MAX: int = 512


@dataclass(frozen=True)
class ContactLink:
    """A contact as a link. `sources` is empty for contacts saved before sources were kept."""

    url: str
    kind: str
    sources: tuple[str, ...] = ()


def _digits_of_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else None


def _nickname(text: str) -> str | None:
    match = _NICKNAME_RE.fullmatch(text.strip())
    return match.group(1).strip(".") if match else None


def _from_url(text: str) -> str | None:
    """A link of a known network in its stored form; any other web link without its query."""
    candidate = text.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme not in ("http", "https") or "." not in host or " " in candidate:
        return None
    path = parts.path.strip("/")
    known = _HOSTS.get(host)
    if known is None:
        return f"https://{host}/{path}" if path else f"https://{host}"
    kind, stored_host = known
    if kind == KIND_WHATSAPP:
        # 'api.whatsapp.com/send?phone=7...' carries the number in its query, 'wa.me/7...' in its path.
        phone = parse_qs(parts.query).get("phone", [""])[0] or path.split("/", 1)[0]
        digits = _digits_of_phone(phone)
        return f"https://wa.me/{digits}" if digits else None
    first = path.split("/", 1)[0]
    if not first:
        return None
    # Nicknames on these networks do not depend on case.
    return f"https://{stored_host}/{first.lower()}"


def contact_link(contact_type: str | None, value: str | None) -> str | None:
    """
    The link for one contact, or None if it cannot be one.

    The type of 2GIS helps with what is not a link: '@name' for Telegram or Instagram, a number for WhatsApp.
    """
    text = (value or "").strip()
    if not text or len(text) > _LINK_MAX:
        return None
    kind = _TYPE_KINDS.get((contact_type or "").lower().strip())
    # 'mailto:a@b.ru?subject=...' is the address a@b.ru.
    email = normalize_email(text.split("?", 1)[0])
    if email is not None and kind in (None, KIND_EMAIL):
        return f"mailto:{email}"
    if kind == KIND_EMAIL:
        return None
    # For a network a link always has a path: 'smile.spb' there is a nickname, not a site.
    looks_like_url = "/" in text if kind is not None else ("/" in text or "." in text)
    if not text.startswith("@") and looks_like_url:
        link = _from_url(text)
        if link is not None:
            return link
    if kind == KIND_WHATSAPP:
        digits = _digits_of_phone(text)
        return f"https://wa.me/{digits}" if digits else None
    nickname = _nickname(text)
    if nickname is None or nickname.lower() in _NETWORK_NAMES:
        return None
    hosts = {KIND_TELEGRAM: "t.me", KIND_INSTAGRAM: "instagram.com", KIND_VK: "vk.com"}
    return f"https://{hosts[kind]}/{nickname.lower()}" if kind in hosts else None


def link_kind(url: str) -> str:
    if url.startswith("mailto:"):
        return KIND_EMAIL
    host = (urlsplit(url).hostname or "").lower()
    known = _HOSTS.get(host)
    return known[0] if known else KIND_OTHER


def links_from_card(card: dict[str, Any]) -> list[str]:
    """
    Links to the networks and messengers of a card of 2GIS, emails not included.

    Fallbacks: the url, the value, then the text shown (the last may be only a nickname); a type 2GIS
    has not used yet whose value is a link to a messenger. A link to any other site is the site, not a contact.
    """
    links: list[str] = []
    groups = card.get("contact_groups")
    for group in groups if isinstance(groups, list) else []:
        contacts = group.get("contacts") if isinstance(group, dict) else None
        for contact in contacts if isinstance(contacts, list) else []:
            if not isinstance(contact, dict):
                continue
            contact_type = str(contact.get("type") or "").lower().strip()
            if contact_type in ("email", "phone"):
                continue
            for key in ("url", "value", "text"):
                link = contact_link(contact_type, str(contact.get(key) or ""))
                if link is None:
                    continue
                kind = link_kind(link)
                # A website of the organization is not a contact link; only networks and messengers are.
                if kind != KIND_OTHER or contact_type in _OTHER_SOCIAL_TYPES:
                    links.append(link)
                break
    return unique_links(links)


def link_key(url: str) -> str:
    return url.rstrip("/").lower()


def unique_links(links: list[str]) -> list[str]:
    """The same link once, in the order first seen."""
    seen: dict[str, str] = {}
    for link in links:
        seen.setdefault(link_key(link), link)
    return list(seen.values())[:_MAX_LINKS]


def parse_stored_links(stored: str | None) -> list[ContactLink]:
    """The column contact_links: a list of {url, sources}. Anything unreadable counts as nothing saved."""
    try:
        items = json.loads(stored) if stored else []
    except ValueError:
        return []
    links: list[ContactLink] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        if not url.startswith(("https://", "mailto:")):
            continue  # only what this module wrote: nothing else becomes a link in a message
        sources = tuple(source for source in item.get("sources") or [] if source in KNOWN_SOURCES)
        links.append(ContactLink(url=url, kind=link_kind(url), sources=sources))
    return links


def merge_contact_links(stored: str | None, links: list[str], source: str) -> str | None:
    """Add links from one source. Nothing saved is removed or emptied; the same link gets one more source."""
    merged: dict[str, dict[str, Any]] = {}
    for item in parse_stored_links(stored):
        merged[link_key(item.url)] = {"url": item.url, "sources": list(item.sources)}
    for link in links:
        entry = merged.setdefault(link_key(link), {"url": link, "sources": []})
        if source in KNOWN_SOURCES and source not in entry["sources"]:
            entry["sources"].append(source)
    if not merged:
        return None
    return json.dumps(list(merged.values())[:_MAX_LINKS], ensure_ascii=False, separators=(",", ":"))


def lead_contacts(stored: str | None, email: str | None, socials: str | None) -> list[ContactLink]:
    """What to show: the links with sources, then contacts saved earlier without them (from a collection)."""
    links = parse_stored_links(stored)
    known = {link_key(link.url) for link in links}
    legacy = [contact_link(None, email)] + [contact_link(None, part) for part in (socials or "").split(",")]
    for url in legacy:
        if url is not None and link_key(url) not in known:
            known.add(link_key(url))
            links.append(ContactLink(url=url, kind=link_kind(url)))
    return links[:_MAX_LINKS]
