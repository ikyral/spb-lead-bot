"""
The file that carries leads from a collector to the server: its format, in both directions.

Reading treats the file as untrusted input: it may be damaged, come from something else, or
contain anything in its strings. Output is escaped separately, where messages are built.
"""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from src.database.models import Lead
from src.services.dedup import coords_from_map_url
from src.services.normalize import normalize_place
from src.services.providers.base import RawPlace

SCHEMA_VERSION: int = 1

REASON_NOT_OBJECT = "запись не является объектом"
REASON_NO_IDENTITY = "нет source, source_id или name"
REASON_BAD_TYPE = "поле неверного типа"
REASON_TOO_LONG = "слишком длинное поле"
REASON_BAD_COORDS = "неверные координаты"
REASON_BAD_DATE = "неверная дата fetched_at"
REASON_NO_CONTACTS = "нет телефона и сайта"

_NAME_MAX = 255
_ADDRESS_MAX = 512
_RUBRIC_MAX = 128
_DISTRICT_MAX = 128
_URL_MAX = 512
_EMAIL_MAX = 255
_PHONE_MAX = 64
_PHONES_MAX_ITEMS = 10
_SOCIAL_MAX = 256
_SOCIALS_MAX_ITEMS = 5
_SOCIALS_TOTAL_MAX = 512
_DATE_MAX = 40
_DEFAULT_RUBRIC = "Без рубрики"

_SOURCE_RE = re.compile(r"[a-z0-9_]{1,32}", re.ASCII)
_SOURCE_ID_RE = re.compile(r"[\w.\-]{1,90}", re.ASCII)
_OTHER_SCHEME_RE = re.compile(r"(?!https?://)[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_SEPARATORS_RE = re.compile(r"[,;]")
_CITY_NAMES: dict[str, str] = {"spb": "Санкт-Петербург"}


class LeadFileError(Exception):
    """The file as a whole cannot be used. The message is meant for the admin."""


# ---------- writing ----------


def _split_list(value: str | None) -> list[str]:
    return [part.strip() for part in _SEPARATORS_RE.split(value or "") if part.strip()]


def lead_to_record(lead: Lead, city_slug: str) -> dict[str, Any]:
    """One lead as it appears in the file."""
    coords = coords_from_map_url(lead.map_url)
    return {
        "source": lead.source,
        "source_id": lead.external_id.removeprefix(f"{lead.source}_"),
        "name": lead.name,
        "address": lead.address,
        "city": _CITY_NAMES.get(city_slug, city_slug),
        "phones": _split_list(lead.phone),
        "website": lead.raw_website,
        "email": lead.email,
        "rubric": lead.category,
        "lat": coords[0] if coords else None,
        "lon": coords[1] if coords else None,
        "source_url": lead.source_url,
        "fetched_at": lead.created_at.isoformat() if lead.created_at else None,
        # Not in the minimal list, but they would be lost otherwise.
        "socials": _split_list(lead.socials),
        "district": lead.district,
    }


def compute_checksum(records: list[Any]) -> str:
    """SHA-256 of the leads in a canonical form, so that file formatting does not matter."""
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_document(records: list[dict[str, Any]], exported_at: datetime, source_host: str | None) -> dict[str, Any]:
    """The whole file. It holds only public data of organizations, no tokens or settings."""
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at.isoformat(),
        "source_host": source_host,
        "count": len(records),
        "checksum": compute_checksum(records),
        "leads": records,
    }


# ---------- reading ----------


@dataclass
class ParsedFile:
    """What could be used from a file, and why the rest could not."""

    total: int = 0
    places: list[RawPlace] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)


class _RejectedRecord(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def clean_text(value: str) -> str:
    """Remove control and invisible characters (including text direction overrides), collapse whitespace."""
    kept = (" " if char in "\t\n\r" else char for char in value)
    return " ".join("".join(char for char in kept if not unicodedata.category(char).startswith("C")).split())


def _text(record: dict[str, Any], key: str, max_length: int) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RejectedRecord(REASON_BAD_TYPE)
    cleaned = clean_text(value)
    if len(cleaned) > max_length:
        raise _RejectedRecord(REASON_TOO_LONG)
    return cleaned or None


def _text_list(record: dict[str, Any], key: str, max_items: int, max_length: int) -> list[str]:
    value = record.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _RejectedRecord(REASON_BAD_TYPE)
    if len(value) > max_items:
        raise _RejectedRecord(REASON_TOO_LONG)
    items = [clean_text(item) for item in value]
    if any(len(item) > max_length for item in items):
        raise _RejectedRecord(REASON_TOO_LONG)
    return [item for item in items if item]


def _url(record: dict[str, Any], key: str) -> str | None:
    """A link in another scheme (javascript:, data:, file:) or an unparsable one is dropped."""
    value = _text(record, key, _URL_MAX)
    if value is None or " " in value or _OTHER_SCHEME_RE.match(value):
        return None
    try:
        urlsplit(value)
    except ValueError:
        return None
    return value


def _coordinate(record: dict[str, Any], key: str, limit: float) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > limit:
        raise _RejectedRecord(REASON_BAD_COORDS)
    return float(value)


def _parse_record(record: Any) -> RawPlace:
    if not isinstance(record, dict):
        raise _RejectedRecord(REASON_NOT_OBJECT)

    source = _text(record, "source", 32)
    source_id = _text(record, "source_id", 90)
    name = _text(record, "name", _NAME_MAX)
    if (
        not source
        or not source_id
        or not name
        or not _SOURCE_RE.fullmatch(source)
        or not _SOURCE_ID_RE.fullmatch(source_id)
    ):
        raise _RejectedRecord(REASON_NO_IDENTITY)

    fetched_at = _text(record, "fetched_at", _DATE_MAX)
    if fetched_at is not None:
        try:
            datetime.fromisoformat(fetched_at)
        except ValueError:
            raise _RejectedRecord(REASON_BAD_DATE) from None

    lat = _coordinate(record, "lat", 90.0)
    lon = _coordinate(record, "lon", 180.0)
    phones = _text_list(record, "phones", _PHONES_MAX_ITEMS, _PHONE_MAX)
    socials = _text_list(record, "socials", _SOCIALS_MAX_ITEMS, _SOCIAL_MAX)
    socials_text = ", ".join(socials)
    if len(socials_text) > _SOCIALS_TOTAL_MAX:
        raise _RejectedRecord(REASON_TOO_LONG)
    website = _url(record, "website")
    # The city is not stored: the bot works with one city and the address names the place.
    _text(record, "city", _DISTRICT_MAX)

    if not phones and not website:
        raise _RejectedRecord(REASON_NO_CONTACTS)

    return normalize_place(
        RawPlace(
            external_id=f"{source}_{source_id}",
            source=source,
            name=name,
            category=_text(record, "rubric", _RUBRIC_MAX) or _DEFAULT_RUBRIC,
            district=_text(record, "district", _DISTRICT_MAX),
            address=_text(record, "address", _ADDRESS_MAX),
            phone=", ".join(phones) or None,
            socials=socials_text or None,
            raw_website=website,
            # The same form the providers use: duplicates are matched by coordinates from this link.
            map_url=None if lat is None or lon is None else f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map",
            email=_text(record, "email", _EMAIL_MAX),
            source_url=_url(record, "source_url"),
        )
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"not allowed in JSON: {value}")


def _load_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
    except UnicodeDecodeError:
        raise LeadFileError("файл не в кодировке UTF-8") from None
    except (ValueError, RecursionError):
        raise LeadFileError("файл повреждён: это не корректный JSON") from None


def parse_lead_file(data: bytes, max_bytes: int, max_leads: int) -> ParsedFile:
    """
    Check a file and pick out the usable leads.

    A file that is not right as a whole raises LeadFileError; a bad record only counts as rejected.
    """
    if len(data) > max_bytes:
        raise LeadFileError(f"файл больше допустимых {max_bytes // 1024} КБ")

    document = _load_json(data)
    if not isinstance(document, dict) or "schema_version" not in document or "leads" not in document:
        raise LeadFileError("это не файл выгрузки лидов: нет schema_version или leads")

    version = document["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        shown = clean_text(str(version))[:20]
        raise LeadFileError(f"неизвестная версия схемы {shown}, бот понимает версию {SCHEMA_VERSION}")

    records = document["leads"]
    if not isinstance(records, list):
        raise LeadFileError("это не файл выгрузки лидов: leads не является списком")
    if len(records) > max_leads:
        raise LeadFileError(f"в файле {len(records)} записей, допустимо не больше {max_leads}")

    count = document.get("count")
    if count is not None and (isinstance(count, bool) or count != len(records)):
        raise LeadFileError("файл повреждён: count не совпадает с числом записей")
    checksum = document.get("checksum")
    if checksum is not None and checksum != compute_checksum(records):
        raise LeadFileError("файл повреждён: контрольная сумма не совпадает")

    parsed = ParsedFile(total=len(records))
    for record in records:
        try:
            parsed.places.append(_parse_record(record))
        except _RejectedRecord as exc:
            parsed.rejected[exc.reason] += 1
    return parsed
