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
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from src.database.models import Lead
from src.services.dedup import coords_from_map_url
from src.services.normalize import normalize_place
from src.services.providers.base import RawPlace
from src.services.twogis_links import TWOGIS_SOURCES, readable_card_number

SCHEMA_VERSION: int = 1
# Files of the enrichment: the bot asks about leads, a collector answers from their cards.
ENRICH_SCHEMA_VERSION: int = 1
KIND_ENRICH_REQUEST: str = "enrich_request"
KIND_ENRICH_RESULT: str = "enrich_result"
# More than this at once is pointless: a person passes captchas, and a collector reads 30 cards per run.
ENRICH_REQUEST_MAX_LEADS: int = 60

REASON_NOT_OBJECT = "запись не является объектом"
REASON_NO_IDENTITY = "нет source, source_id или name"
REASON_BAD_TYPE = "поле неверного типа"
REASON_TOO_LONG = "слишком длинное поле"
REASON_BAD_COORDS = "неверные координаты"
REASON_BAD_DATE = "неверная дата fetched_at"
REASON_NO_CONTACTS = "нет телефона и сайта"
REASON_NO_PARSED_FLAG = "нет признака parsed"
REASON_BAD_WEBSITE = "адрес сайта непригоден"
REASON_BAD_CARD_LINK = "ссылка не ведёт на карточку этой организации в 2ГИС"

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
        # Optional, an older server ignores it: the source's own category.
        "rubric_name": lead.rubric,
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


def _parse_record(record: Any, require_contact: bool) -> RawPlace:
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

    no_contacts = not phones and not website
    # Without contacts an organization from 2GIS is still a lead: the owner looks in its card.
    if no_contacts and (require_contact or source not in TWOGIS_SOURCES):
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
            rubric=_text(record, "rubric_name", _NAME_MAX),
            contacts_known=not no_contacts,
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


def parse_lead_file(data: bytes, max_bytes: int, max_leads: int, require_contact: bool = False) -> ParsedFile:
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
            parsed.places.append(_parse_record(record, require_contact))
        except _RejectedRecord as exc:
            parsed.rejected[exc.reason] += 1
    return parsed


# ---------- the enrichment: a request from the bot and a result from a collector ----------


@dataclass(frozen=True)
class EnrichRequestItem:
    """A lead whose card a collector is asked to read."""

    source: str
    source_id: str
    source_url: str
    firm_number: str


@dataclass
class EnrichRequest:
    """What a collector may open from a request, and why the rest cannot be opened."""

    total: int = 0
    items: list[EnrichRequestItem] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class EnrichRecord:
    """What the card of a lead said. `parsed=False` says nothing about the organization."""

    source: str
    source_id: str
    parsed: bool
    fetched_at: datetime
    phones: list[str] = field(default_factory=list)
    website: str | None = None
    socials: list[str] = field(default_factory=list)
    email: str | None = None

    @property
    def external_id(self) -> str:
        return f"{self.source}_{self.source_id}"


@dataclass
class EnrichResult:
    """The usable records of a result file, and why the rest cannot be used."""

    total: int = 0
    records: list[EnrichRecord] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)


def enrich_request_record(lead: Lead) -> dict[str, Any]:
    """One lead in a request: enough to find its card, and nothing else."""
    return {
        "source": lead.source,
        "source_id": lead.external_id.removeprefix(f"{lead.source}_"),
        "source_url": lead.source_url,
        "name": lead.name,
        "district": lead.district,
    }


def enrich_result_record(
    source: str,
    source_id: str,
    fetched_at: datetime,
    parsed: bool = False,
    phones: list[str] | None = None,
    website: str | None = None,
    socials: list[str] | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """One lead in a result. Each has the same fields, whether the card was recognized or not."""
    return {
        "source": source,
        "source_id": source_id,
        "parsed": parsed,
        "phones": phones or [],
        "website": website,
        "socials": socials or [],
        "email": email,
        "fetched_at": fetched_at.isoformat(),
    }


def build_enrich_document(kind: str, records: list[dict[str, Any]], exported_at: datetime) -> dict[str, Any]:
    """A whole file of the enrichment. Public data of organizations only."""
    return {
        "schema_version": ENRICH_SCHEMA_VERSION,
        "kind": kind,
        "exported_at": exported_at.isoformat(),
        "count": len(records),
        "checksum": compute_checksum(records),
        "records": records,
    }


def file_kind(data: bytes, max_bytes: int) -> str | None:
    """What a file says it is. None for the older files with leads, which say nothing."""
    if len(data) > max_bytes:
        raise LeadFileError(f"файл больше допустимых {max_bytes // 1024} КБ")
    document = _load_json(data)
    kind = document.get("kind") if isinstance(document, dict) else None
    if kind is None or isinstance(kind, str):
        return kind
    raise LeadFileError("неизвестный тип файла: поле kind не является строкой")


def _enrich_records(data: bytes, max_bytes: int, max_records: int, kind: str) -> list[Any]:
    """The records of a file of this kind. Here, unlike in the older format, nothing may be left out."""
    if file_kind(data, max_bytes) != kind:
        raise LeadFileError(f"это не файл {kind}: другое значение kind")
    document = _load_json(data)

    version = document.get("schema_version")
    if isinstance(version, bool) or version != ENRICH_SCHEMA_VERSION:
        shown = clean_text(str(version))[:20]
        raise LeadFileError(f"неизвестная версия схемы {shown}, понятна версия {ENRICH_SCHEMA_VERSION}")

    records = document.get("records")
    if not isinstance(records, list):
        raise LeadFileError("файл повреждён: нет списка records")
    if len(records) > max_records:
        raise LeadFileError(f"в файле {len(records)} записей, допустимо не больше {max_records}")
    count = document.get("count")
    if isinstance(count, bool) or count != len(records):
        raise LeadFileError("файл повреждён: count не совпадает с числом записей")
    if document.get("checksum") != compute_checksum(records):
        raise LeadFileError("файл повреждён: контрольная сумма не совпадает")
    return records


def _identity(record: Any) -> tuple[str, str]:
    if not isinstance(record, dict):
        raise _RejectedRecord(REASON_NOT_OBJECT)
    source = _text(record, "source", 32)
    source_id = _text(record, "source_id", 90)
    if not source or not source_id or not _SOURCE_RE.fullmatch(source) or not _SOURCE_ID_RE.fullmatch(source_id):
        raise _RejectedRecord(REASON_NO_IDENTITY)
    return source, source_id


def _parse_request_item(record: Any) -> EnrichRequestItem:
    source, source_id = _identity(record)
    # A browser will open this link: the file must not be able to send it anywhere else.
    source_url = _text(record, "source_url", _URL_MAX)
    number = readable_card_number(source_url)
    if source_url is None or number is None or source_id.split("_", 1)[0] != number or source not in TWOGIS_SOURCES:
        raise _RejectedRecord(REASON_BAD_CARD_LINK)
    return EnrichRequestItem(source=source, source_id=source_id, source_url=source_url, firm_number=number)


def parse_enrich_request(data: bytes, max_bytes: int) -> EnrichRequest:
    """Check a request on a collector. A file that is not right as a whole raises LeadFileError."""
    records = _enrich_records(data, max_bytes, ENRICH_REQUEST_MAX_LEADS, KIND_ENRICH_REQUEST)
    request = EnrichRequest(total=len(records))
    for record in records:
        try:
            request.items.append(_parse_request_item(record))
        except _RejectedRecord as exc:
            request.rejected[exc.reason] += 1
    return request


def _fetched_at(record: dict[str, Any]) -> datetime:
    """Required here: it tells a fresh result from an old file sent again."""
    text = _text(record, "fetched_at", _DATE_MAX)
    try:
        moment = datetime.fromisoformat(text or "")
    except ValueError:
        raise _RejectedRecord(REASON_BAD_DATE) from None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse_result_record(record: Any) -> EnrichRecord:
    source, source_id = _identity(record)
    parsed = record.get("parsed")
    if not isinstance(parsed, bool):
        raise _RejectedRecord(REASON_NO_PARSED_FLAG)
    fetched_at = _fetched_at(record)
    if not parsed:
        return EnrichRecord(source=source, source_id=source_id, parsed=False, fetched_at=fetched_at)

    phones = _text_list(record, "phones", _PHONES_MAX_ITEMS, _PHONE_MAX)
    socials = _text_list(record, "socials", _SOCIALS_MAX_ITEMS, _SOCIAL_MAX)
    if len(", ".join(socials)) > _SOCIALS_TOTAL_MAX:
        raise _RejectedRecord(REASON_TOO_LONG)
    website = _url(record, "website")
    # Dropping it, as a lead file may, would turn an unusable address into 'there is no site'.
    if website is None and _text(record, "website", _URL_MAX):
        raise _RejectedRecord(REASON_BAD_WEBSITE)
    return EnrichRecord(
        source=source,
        source_id=source_id,
        parsed=True,
        fetched_at=fetched_at,
        phones=phones,
        website=website,
        socials=socials,
        email=_text(record, "email", _EMAIL_MAX),
    )


def fit_result_lists(phones: list[str], socials: list[str]) -> tuple[list[str], list[str]]:
    """As much as the bot accepts: a card with many networks is a card that was read, not a bad record."""
    kept: list[str] = []
    for social in socials:
        if len(social) <= _SOCIAL_MAX and len(", ".join([*kept, social])) <= _SOCIALS_TOTAL_MAX:
            kept.append(social)
    return [phone for phone in phones if len(phone) <= _PHONE_MAX][:_PHONES_MAX_ITEMS], kept[:_SOCIALS_MAX_ITEMS]


def is_valid_result_record(record: dict[str, Any]) -> bool:
    """Would the bot accept this record? A collector asks before saying that a card was read."""
    try:
        _parse_result_record(record)
    except _RejectedRecord:
        return False
    return True


def parse_enrich_result(data: bytes, max_bytes: int, max_records: int) -> EnrichResult:
    """Check a result on the server. A file that is not right as a whole raises LeadFileError."""
    records = _enrich_records(data, max_bytes, max_records, KIND_ENRICH_RESULT)
    result = EnrichResult(total=len(records))
    for record in records:
        try:
            result.records.append(_parse_result_record(record))
        except _RejectedRecord as exc:
            result.rejected[exc.reason] += 1
    return result
