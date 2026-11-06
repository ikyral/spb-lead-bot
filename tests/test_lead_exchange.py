"""Tests for the export file: writing it on a collector, checking it and importing it on the server."""

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Document, Message, Update, User
from sqlalchemy import func, select

from src.bot.formatters import format_import_report, format_issued_lead
from src.bot.handlers import imports
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import Settings
from src.database.models import CONTACT_REPLIED, STATUS_ISSUED, STATUS_NEW, ImportLog, Lead
from src.database.repo import (
    count_imported_leads,
    count_new_leads,
    get_last_import,
    issue_next_leads,
    set_contact_status,
)
from src.services import lead_import, pipeline
from src.services.lead_export import export_new_leads
from src.services.lead_file import (
    REASON_BAD_COORDS,
    REASON_BAD_DATE,
    REASON_BAD_TYPE,
    REASON_NO_CONTACTS,
    REASON_NO_IDENTITY,
    REASON_NOT_OBJECT,
    REASON_TOO_LONG,
    SCHEMA_VERSION,
    LeadFileError,
    build_document,
    clean_text,
    compute_checksum,
    parse_lead_file,
)
from src.services.lead_import import REASON_NOT_QUALIFIED, SOURCE_INBOX, SOURCE_TELEGRAM, ImportResult, ImportService

MAX_BYTES = 1024 * 1024
MAX_LEADS = 100
ADMIN, STRANGER = 100, 999
GROUP = -1001


def _record(number: int = 1, **overrides) -> dict:
    record = {
        "source": "2gis",
        "source_id": f"7000{number}",
        "name": f"Организация {number}",
        "address": f"Невский проспект, {number}",
        "city": "Санкт-Петербург",
        "phones": [f"+7 812 555-{number:02d}-00"],
        "website": None,
        "email": None,
        "rubric": "dentist_clinic",
        "lat": 59.93,
        "lon": 30.3 + number / 100,
        "source_url": f"https://2gis.ru/firm/7000{number}",
        "fetched_at": "2026-11-05T10:00:00+00:00",
    }
    record.update(overrides)
    return record


def _file(records: list, **overrides) -> bytes:
    document = build_document(records, datetime(2026, 11, 5, tzinfo=UTC), "home-pc")
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _parse(data: bytes, max_bytes: int = MAX_BYTES, max_leads: int = MAX_LEADS):
    return parse_lead_file(data, max_bytes, max_leads)


def _lead(number: int, **overrides) -> Lead:
    values = {
        "external_id": f"2gis_7000{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "phone": f"+78125550{number:02d}0",
        "site_status": "no_site",
        "score": 50,
        "crm_status": STATUS_NEW,
        "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(minutes=number),
    }
    values.update(overrides)
    return Lead(**values)


@pytest.fixture(autouse=True)
def _website_checks(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """No network: everything counts as having no site, unless the address says it has one."""
    checked: list[str | None] = []

    async def fake_analyze(url, client=None):
        checked.append(url)
        if url and "working" in url:
            return "has_site", None
        return ("dead_site", None) if url and "dead" in url else ("no_site", None)

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)
    return checked


# ---------- export ----------


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def test_export_writes_new_leads_and_marks_them(session_maker, tmp_path: Path) -> None:
    await _add(session_maker, _lead(1), _lead(2))

    result = await export_new_leads(session_maker, tmp_path / "exports", "spb", MAX_LEADS)

    assert result.count == 2
    assert len(result.files) == 1
    assert re.fullmatch(r"leads_\d{8}_\d{6}\.json", result.files[0].name)
    assert [path.name for path in (tmp_path / "exports").iterdir()] == [result.files[0].name]  # no temporary files
    async with session_maker() as session:
        exported = (await session.scalars(select(Lead).where(Lead.exported_at.is_not(None)))).all()
    assert len(exported) == 2


async def test_second_export_takes_only_what_was_not_exported(session_maker, tmp_path: Path) -> None:
    await _add(session_maker, _lead(1))
    await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)

    nothing = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    assert (nothing.count, nothing.files) == (0, [])

    await _add(session_maker, _lead(2))
    result = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    document = json.loads(result.files[0].read_text(encoding="utf-8"))
    assert [lead["name"] for lead in document["leads"]] == ["Организация 2"]


async def test_export_splits_files_at_the_import_limit(session_maker, tmp_path: Path) -> None:
    await _add(session_maker, *(_lead(number) for number in range(1, 6)))
    result = await export_new_leads(session_maker, tmp_path, "spb", max_leads_per_file=2)
    assert result.count == 5
    assert len(result.files) == 3
    assert len({path.name for path in result.files}) == 3
    for path in result.files:
        assert _parse(path.read_bytes(), max_leads=2).total <= 2


async def test_failed_write_leaves_leads_unexported(session_maker, tmp_path: Path, monkeypatch) -> None:
    await _add(session_maker, _lead(1))

    def fail(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr("src.services.lead_export._write_file", fail)
    with pytest.raises(OSError, match="disk is full"):
        await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    monkeypatch.undo()
    result = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    assert result.count == 1


async def test_file_format(session_maker, tmp_path: Path) -> None:
    lead = _lead(
        1,
        address="Невский проспект, 10",
        phone="+78125550101, +79210000002",
        raw_website="smile.example",
        email="info@smile.example",
        socials="https://vk.com/smile, https://t.me/smile",
        district="Центральный",
        map_url="https://yandex.ru/maps/?pt=30.325,59.935&z=17&l=map",
        source_url="https://2gis.ru/firm/70001",
    )
    await _add(session_maker, lead)

    result = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    document = json.loads(result.files[0].read_text(encoding="utf-8"))

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["count"] == 1
    assert datetime.fromisoformat(document["exported_at"]).tzinfo is not None
    assert document["source_host"]
    assert document["checksum"] == compute_checksum(document["leads"])
    assert document["leads"] == [
        {
            "source": "2gis",
            "source_id": "70001",
            "name": "Организация 1",
            "address": "Невский проспект, 10",
            "city": "Санкт-Петербург",
            "phones": ["+78125550101", "+79210000002"],
            "website": "smile.example",
            "email": "info@smile.example",
            "rubric": "dentist_clinic",
            "rubric_name": None,
            "lat": 59.935,
            "lon": 30.325,
            "source_url": "https://2gis.ru/firm/70001",
            "fetched_at": "2026-11-05T10:01:00+00:00",
            "socials": ["https://vk.com/smile", "https://t.me/smile"],
            "district": "Центральный",
        }
    ]


async def test_file_contains_no_secrets_or_bot_settings(session_maker, tmp_path: Path) -> None:
    await _add(session_maker, _lead(1))
    result = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    text = result.files[0].read_text(encoding="utf-8").lower()
    for word in ("token", "api_key", "secret", "password", "admin", "crm_status"):
        assert word not in text


# ---------- reading a file ----------


def test_valid_file_is_read() -> None:
    parsed = _parse(_file([_record(1, lat=59.93, lon=30.31), _record(2)]))
    assert parsed.total == 2
    assert not parsed.rejected
    place = parsed.places[0]
    assert (place.external_id, place.source, place.category) == ("2gis_70001", "2gis", "dentist_clinic")
    assert place.phone == "+78125550100"  # normalized like any collected place
    assert place.map_url == "https://yandex.ru/maps/?pt=30.31,59.93&z=17&l=map"  # usable for duplicate detection


def test_file_without_optional_top_level_fields_and_with_bom_is_read() -> None:
    data = json.dumps({"schema_version": 1, "leads": [_record(1)]}).encode("utf-8-sig")
    assert len(_parse(data).places) == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"{not json", "не корректный JSON"),
        (b"", "не корректный JSON"),
        (b"\xff\xfe\x00bad", "UTF-8"),
        (b"[" * 200000, "не корректный JSON"),  # nesting bomb
        (b'{"schema_version": 1, "leads": [{"lat": NaN}]}', "не корректный JSON"),
        (b"[1, 2, 3]", "не файл выгрузки"),
        (b'{"foo": "bar"}', "не файл выгрузки"),
        (b'{"schema_version": 1}', "не файл выгрузки"),
        (b'{"leads": []}', "не файл выгрузки"),
        (b'{"schema_version": 1, "leads": "many"}', "не является списком"),
        (b'{"schema_version": 2, "leads": []}', "неизвестная версия схемы 2"),
        (b'{"schema_version": "1", "leads": []}', "неизвестная версия"),
        (b'{"schema_version": true, "leads": []}', "неизвестная версия"),
        (b'{"schema_version": 1, "count": 5, "leads": []}', "count не совпадает"),
        (b'{"schema_version": 1, "checksum": "abc", "leads": []}', "контрольная сумма"),
    ],
)
def test_unusable_files_are_refused_with_a_clear_message(data: bytes, message: str) -> None:
    with pytest.raises(LeadFileError, match=message):
        _parse(data)


def test_edited_file_fails_the_checksum() -> None:
    document = json.loads(_file([_record(1)]))
    document["leads"][0]["name"] = "Подмена"
    with pytest.raises(LeadFileError, match="контрольная сумма"):
        _parse(json.dumps(document).encode("utf-8"))


def test_too_big_file_and_too_many_records_are_refused() -> None:
    with pytest.raises(LeadFileError, match="больше допустимых"):
        _parse(b"x" * 2048, max_bytes=1024)
    with pytest.raises(LeadFileError, match="3 записей"):
        _parse(_file([_record(number) for number in (1, 2, 3)]), max_leads=2)


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("строка", REASON_NOT_OBJECT),
        ({"name": "Без источника", "phones": ["+78125550101"]}, REASON_NO_IDENTITY),
        (_record(name=None), REASON_NO_IDENTITY),
        (_record(name="   "), REASON_NO_IDENTITY),
        (_record(source="2GIS!"), REASON_NO_IDENTITY),
        (_record(source_id="../../etc/passwd"), REASON_NO_IDENTITY),
        (_record(name=123), REASON_BAD_TYPE),
        (_record(address=["а"]), REASON_BAD_TYPE),
        (_record(phones="+78125550101"), REASON_BAD_TYPE),
        (_record(phones=[1, 2]), REASON_BAD_TYPE),
        (_record(name="я" * 256), REASON_TOO_LONG),
        (_record(address="я" * 513), REASON_TOO_LONG),
        (_record(phones=["+78125550101"] * 11), REASON_TOO_LONG),
        (_record(lat=91), REASON_BAD_COORDS),
        (_record(lon="30.3"), REASON_BAD_COORDS),
        (_record(lat=True), REASON_BAD_COORDS),
        (_record(fetched_at="вчера"), REASON_BAD_DATE),
        (_record(source="osm", phones=[], website=None), REASON_NO_CONTACTS),
    ],
)
def test_bad_record_is_rejected_and_counted_while_good_ones_stay(record: object, reason: str) -> None:
    parsed = _parse(_file([record, _record(2)]))
    assert parsed.total == 2
    assert [place.external_id for place in parsed.places] == ["2gis_70002"]
    assert parsed.rejected == {reason: 1}


def test_control_characters_are_removed() -> None:
    assert clean_text("Ул\x00ыб\x07ка‮​  \n\t ООО") == "Улыбка ООО"
    place = _parse(_file([_record(1, name="Улы\x00бка‮", address="Невский\n10")])).places[0]
    assert (place.name, place.address) == ("Улыбка", "Невский 10")


def test_links_in_other_schemes_are_dropped() -> None:
    record = _record(1, website="javascript:alert(1)", source_url="data:text/html,<script>alert(1)</script>")
    place = _parse(_file([record])).places[0]
    assert place.raw_website is None
    assert place.source_url is None


def test_lead_with_only_a_dangerous_link_has_no_contacts() -> None:
    parsed = _parse(_file([_record(1, source="osm", phones=[], website="javascript:alert(1)")]))
    assert parsed.rejected == {REASON_NO_CONTACTS: 1}


def test_website_without_a_scheme_is_kept() -> None:
    place = _parse(_file([_record(1, phones=[], website="smile.example/?utm_source=2gis")])).places[0]
    assert place.raw_website == "smile.example"


def test_malicious_strings_reach_messages_escaped() -> None:
    evil = '<b>Улыбка</b><a href="tg://x">click</a>&'
    place = _parse(_file([_record(1, name=evil, address=evil)])).places[0]
    lead = Lead(
        external_id=place.external_id,
        source=place.source,
        name=place.name,
        category=place.category,
        address=place.address,
        phone=place.phone,
        site_status="no_site",
        crm_status=STATUS_NEW,
        score=50,
    )
    card = format_issued_lead(lead)
    assert "<b>Улыбка" not in card
    assert 'href="tg://x"' not in card
    assert "&lt;b&gt;Улыбка" in card


def test_import_report_escapes_the_file_name_and_shows_reasons() -> None:
    result = ImportResult(source=SOURCE_TELEGRAM, file_name="<script>.json", total=5, saved_new=1, duplicates=2)
    result.rejected.update({REASON_NO_CONTACTS: 2})
    text = format_import_report(result)
    assert "&lt;script&gt;.json" in text
    assert "<script>" not in text
    assert "В файле: <b>5</b>" in text and "Новых: <b>1</b>" in text and "Дубликатов: <b>2</b>" in text
    assert "Отклонено: <b>2</b>" in text and REASON_NO_CONTACTS in text

    failed = ImportResult(source=SOURCE_INBOX, file_name="x.json", ok=False, error="<версия>")
    assert "&lt;версия&gt;" in format_import_report(failed)


# ---------- importing ----------


@pytest.fixture
def service(session_maker, tmp_path: Path) -> ImportService:
    report = AsyncMock()
    instance = ImportService(session_maker, report, tmp_path / "inbox", MAX_BYTES, MAX_LEADS)
    instance.report_mock = report
    return instance


async def _leads(session_maker) -> list[Lead]:
    async with session_maker() as session:
        return list((await session.scalars(select(Lead).order_by(Lead.id))).all())


async def _lead_states(session_maker) -> dict:
    return {
        lead.id: (lead.crm_status, lead.contact_status, lead.name, lead.issued_at)
        for lead in await _leads(session_maker)
    }


async def test_valid_file_is_imported(service: ImportService, session_maker) -> None:
    result = await service.import_bytes(_file([_record(1), _record(2)]), "leads_1.json", SOURCE_TELEGRAM)

    assert (result.ok, result.total, result.saved_new, result.duplicates, result.rejected_total) == (True, 2, 2, 0, 0)
    assert result.pool_size == 2
    leads = await _leads(session_maker)
    assert [lead.crm_status for lead in leads] == [STATUS_NEW, STATUS_NEW]
    assert leads[0].source_url == "https://2gis.ru/firm/70001"
    assert leads[0].imported_at is not None
    async with session_maker() as session:
        assert await count_imported_leads(session) == 2
    service.report_mock.assert_awaited_once()


async def test_second_import_of_the_same_file_changes_nothing(service, session_maker, _website_checks) -> None:
    data = _file([_record(1), _record(2)])
    await service.import_bytes(data, "leads_1.json", SOURCE_TELEGRAM)
    checks = len(_website_checks)

    again = await service.import_bytes(data, "leads_1.json", SOURCE_INBOX)

    assert (again.ok, again.total, again.saved_new, again.duplicates) == (True, 2, 0, 2)
    assert len(await _leads(session_maker)) == 2
    assert len(_website_checks) == checks  # known leads are not checked again


async def test_issued_and_processed_leads_are_not_touched(service: ImportService, session_maker) -> None:
    await service.import_bytes(_file([_record(1), _record(2)]), "first.json", SOURCE_TELEGRAM)
    async with session_maker() as session:
        issued = await issue_next_leads(session, 2)
        await set_contact_status(session, issued[0].id, CONTACT_REPLIED, ADMIN, "@anna")
    before = await _lead_states(session_maker)

    changed = [_record(1, name="Новое имя", address="Новый адрес"), _record(2)]
    result = await service.import_bytes(_file(changed), "second.json", SOURCE_TELEGRAM)

    assert result.saved_new == 0
    after = await _lead_states(session_maker)
    assert after == before
    assert all(status == STATUS_ISSUED for status, *_ in after.values())
    async with session_maker() as session:
        assert await count_new_leads(session) == 0


@pytest.mark.parametrize(
    "duplicate",
    [
        _record(9, source="osm", source_id="1", phones=["8 (812) 555-01-00"], name="Другое", address="Другой адрес"),
        _record(
            9, source="osm", source_id="1", phones=["+79210000000"], name="организация 1", address="НЕВСКИЙ ПРОСПЕКТ 1"
        ),
        _record(1, phones=["+79210000000"], name="Совсем другое", address="Другой адрес"),
    ],
    ids=["same phone", "same name and address", "same source and id"],
)
async def test_same_rules_of_duplicates_as_collection(service: ImportService, session_maker, duplicate: dict) -> None:
    await service.import_bytes(_file([_record(1)]), "first.json", SOURCE_TELEGRAM)
    result = await service.import_bytes(_file([duplicate]), "second.json", SOURCE_TELEGRAM)
    assert (result.saved_new, result.duplicates) == (0, 1)
    assert len(await _leads(session_maker)) == 1


async def test_duplicates_inside_one_file_are_saved_once(service: ImportService, session_maker) -> None:
    result = await service.import_bytes(_file([_record(1), _record(1)]), "a.json", SOURCE_TELEGRAM)
    assert (result.saved_new, result.duplicates) == (1, 1)


async def test_rejections_are_counted_with_their_reasons(service: ImportService) -> None:
    records = [
        _record(1),
        _record(2, source="osm", phones=[], website=None),
        _record(3, name=None),
        _record(4, phones=[], website="https://working.example"),  # has a working site: not a lead
    ]
    result = await service.import_bytes(_file(records), "a.json", SOURCE_TELEGRAM)
    assert (result.total, result.saved_new, result.duplicates) == (4, 1, 0)
    assert result.rejected == {REASON_NO_CONTACTS: 1, REASON_NO_IDENTITY: 1, REASON_NOT_QUALIFIED: 1}


@pytest.mark.parametrize("data", [b"{broken", b'{"schema_version": 99, "leads": []}', b'{"foo": 1}'])
async def test_bad_file_is_refused_recorded_and_reported(service: ImportService, session_maker, data: bytes) -> None:
    result = await service.import_bytes(data, "bad.json", SOURCE_TELEGRAM)
    assert result.ok is False
    assert result.error
    assert await _leads(session_maker) == []
    service.report_mock.assert_awaited_once()
    async with session_maker() as session:
        last = await get_last_import(session)
    assert (last.ok, last.file_name, last.source) == (False, "bad.json", SOURCE_TELEGRAM)
    assert last.error == result.error


async def test_too_big_file_is_refused(session_maker, tmp_path: Path) -> None:
    service = ImportService(session_maker, AsyncMock(), tmp_path, max_bytes=1024, max_leads=MAX_LEADS)
    result = await service.import_bytes(b"x" * 2000, "big.json", SOURCE_TELEGRAM)
    assert result.ok is False
    assert "больше допустимых" in result.error


async def test_failure_while_saving_does_not_escape_and_saves_nothing(service, session_maker, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(lead_import, "save_collected_places", fail)
    result = await service.import_bytes(_file([_record(1)]), "a.json", SOURCE_TELEGRAM)
    assert result.ok is False
    assert "secret detail" not in result.error
    assert await _leads(session_maker) == []


async def test_failure_of_the_report_does_not_lose_the_result(service: ImportService, session_maker) -> None:
    service.report_mock.side_effect = RuntimeError("telegram is down")
    result = await service.import_bytes(_file([_record(1)]), "a.json", SOURCE_TELEGRAM)
    assert result.saved_new == 1


async def test_import_log_holds_counts_and_not_lead_data(service: ImportService, session_maker) -> None:
    await service.import_bytes(_file([_record(1, name="Секретное имя")]), "a.json", SOURCE_INBOX)
    async with session_maker() as session:
        last = await get_last_import(session)
    assert (last.ok, last.total, last.saved_new, last.source) == (True, 1, 1, SOURCE_INBOX)
    assert "Секретное имя" not in repr(vars(last))


async def test_import_does_not_log_lead_content(service: ImportService, caplog) -> None:
    caplog.set_level("INFO")
    data = _file([_record(1, name="Секретное имя", email="x@secret.example")])
    await service.import_bytes(data, "a.json", SOURCE_INBOX)
    assert "Секретное имя" not in caplog.text
    assert "secret.example" not in caplog.text


async def test_parallel_imports_from_telegram_and_the_folder_create_no_duplicates(service, session_maker) -> None:
    data = _file([_record(number) for number in range(1, 11)])
    results = await asyncio.gather(
        service.import_bytes(data, "a.json", SOURCE_TELEGRAM),
        service.import_bytes(data, "a.json", SOURCE_INBOX),
        service.import_bytes(data, "a.json", SOURCE_TELEGRAM),
    )
    assert sorted(result.saved_new for result in results) == [0, 0, 10]
    assert len(await _leads(session_maker)) == 10


async def test_overlapping_files_with_different_ids_do_not_duplicate_in_parallel(service, session_maker) -> None:
    """Cross-source duplicates need to see each other's result: that is why imports go one at a time."""
    first = _file([_record(1, source="2gis", source_id="1")])
    second = _file([_record(1, source="osm", source_id="2")])
    await asyncio.gather(
        service.import_bytes(first, "a.json", SOURCE_TELEGRAM),
        service.import_bytes(second, "b.json", SOURCE_INBOX),
    )
    assert len(await _leads(session_maker)) == 1


async def test_export_then_import_end_to_end(session_maker, tmp_path: Path) -> None:
    """What a collector writes, the server reads, even with its own database."""
    collector_leads = [
        _lead(
            1,
            address="Невский, 1",
            map_url="https://yandex.ru/maps/?pt=30.3,59.9&z=17&l=map",
            socials="https://vk.com/a",
        ),
        _lead(2, phone=None, raw_website="https://dead.example", site_status="dead_site"),
    ]
    await _add(session_maker, *collector_leads)
    exported = await export_new_leads(session_maker, tmp_path, "spb", MAX_LEADS)
    data = exported.files[0].read_bytes()

    async with session_maker() as session:  # the same file, as if on an empty server
        for lead in await session.scalars(select(Lead)):
            await session.delete(lead)
        await session.commit()

    service = ImportService(session_maker, AsyncMock(), tmp_path, MAX_BYTES, MAX_LEADS)
    result = await service.import_bytes(data, exported.files[0].name, SOURCE_TELEGRAM)

    assert (result.ok, result.saved_new) == (True, 2)
    leads = await _leads(session_maker)
    assert leads[0].external_id == "2gis_70001"
    assert leads[0].map_url == "https://yandex.ru/maps/?pt=30.3,59.9&z=17&l=map"
    assert leads[0].socials == "https://vk.com/a"
    assert leads[1].raw_website == "https://dead.example"


# ---------- the inbox folder ----------


def _put(inbox: Path, name: str, data: bytes, age_seconds: float = 3600) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_bytes(data)
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))
    return path


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    return tmp_path / "inbox"


async def test_good_file_goes_to_done(service: ImportService, session_maker, inbox: Path) -> None:
    _put(inbox, "leads_1.json", _file([_record(1)]))

    assert await service.scan_inbox() == 1

    assert not (inbox / "leads_1.json").exists()
    assert (inbox / "done" / "leads_1.json").exists()
    assert len(await _leads(session_maker)) == 1
    service.report_mock.assert_awaited_once()


async def test_bad_file_goes_to_failed_with_the_reason(service: ImportService, inbox: Path, caplog) -> None:
    _put(inbox, "broken.json", b"{not json")

    await service.scan_inbox()

    assert not (inbox / "broken.json").exists()
    assert (inbox / "failed" / "broken.json").exists()
    assert "JSON" in (inbox / "failed" / "broken.json.reason.txt").read_text(encoding="utf-8")
    assert "broken.json" in caplog.text
    result: ImportResult = service.report_mock.await_args.args[0]
    assert result.ok is False


async def test_one_bad_file_does_not_stop_the_others(service: ImportService, session_maker, inbox: Path) -> None:
    _put(inbox, "a_broken.json", b"{")
    _put(inbox, "b_good.json", _file([_record(1)]))
    assert await service.scan_inbox() == 2
    assert (inbox / "failed" / "a_broken.json").exists()
    assert (inbox / "done" / "b_good.json").exists()
    assert len(await _leads(session_maker)) == 1


async def test_too_big_file_goes_to_failed_without_being_read_whole(session_maker, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    service = ImportService(session_maker, AsyncMock(), inbox, max_bytes=1024, max_leads=MAX_LEADS)
    _put(inbox, "big.json", b"x" * 5000)
    await service.scan_inbox()
    assert (inbox / "failed" / "big.json").exists()


async def test_file_still_being_uploaded_is_left_alone(service: ImportService, inbox: Path) -> None:
    _put(inbox, "uploading.json", b'{"schema_version": 1, "lea', age_seconds=0)
    assert await service.scan_inbox() == 0
    assert (inbox / "uploading.json").exists()
    service.report_mock.assert_not_awaited()


async def test_other_files_and_subfolders_are_ignored(service: ImportService, inbox: Path) -> None:
    _put(inbox, "notes.txt", b"text")
    _put(inbox / "done", "old.json", _file([_record(1)]))
    assert await service.scan_inbox() == 0
    assert (inbox / "notes.txt").exists()


async def test_same_name_later_does_not_overwrite_the_earlier_file(service: ImportService, inbox: Path) -> None:
    _put(inbox, "leads.json", _file([_record(1)]))
    await service.scan_inbox()
    _put(inbox, "leads.json", _file([_record(2)]))
    await service.scan_inbox()
    assert len(list((inbox / "done").glob("leads*.json"))) == 2


async def test_missing_folder_is_created(service: ImportService, inbox: Path) -> None:
    assert await service.scan_inbox() == 0
    assert inbox.is_dir()


async def test_unusable_folder_is_logged_not_raised(session_maker, tmp_path: Path, caplog) -> None:
    occupied = tmp_path / "file_instead_of_folder"
    occupied.write_text("x")
    service = ImportService(session_maker, AsyncMock(), occupied, MAX_BYTES, MAX_LEADS)
    assert await service.scan_inbox() == 0
    assert "inbox" in caplog.text


async def test_file_that_cannot_be_moved_does_not_stop_the_others(service, inbox: Path, monkeypatch, caplog) -> None:
    _put(inbox, "a.json", _file([_record(1)]))
    _put(inbox, "b.json", _file([_record(2)]))
    real_move = lead_import._move_to

    def fail_for_a(path: Path, target_dir: Path, reason: str | None = None) -> Path:
        if path.name == "a.json":
            raise PermissionError("locked")
        return real_move(path, target_dir, reason)

    monkeypatch.setattr(lead_import, "_move_to", fail_for_a)
    assert await service.scan_inbox() == 1  # never raises
    assert (inbox / "done" / "b.json").exists()
    assert "a.json" in caplog.text


# ---------- sending a file to the bot ----------


def _document_update(user_id: int, chat_id: int, file_name: str | None, file_size: int = 100) -> Update:
    message = Message(
        message_id=1,
        date=datetime(2026, 11, 5, tzinfo=UTC),
        chat=Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        document=Document(file_id="f", file_unique_id="u", file_name=file_name, file_size=file_size),
    )
    return Update(update_id=1, message=message)


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.bot.middlewares.settings", Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=ADMIN))
    status = MagicMock()
    status.edit_text = AsyncMock()
    calls = MagicMock()
    calls.status = status
    calls.answer = AsyncMock(return_value=status)
    calls.reply = AsyncMock()
    monkeypatch.setattr(Message, "answer", calls.answer)
    monkeypatch.setattr(Message, "reply", calls.reply)
    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.include_router(imports.router)
    calls.dp = dp
    calls.service = MagicMock()
    calls.service.max_bytes = MAX_BYTES
    calls.service.import_bytes = AsyncMock(return_value=ImportResult(source=SOURCE_TELEGRAM, file_name="a.json"))
    calls.bot = MagicMock()
    calls.bot.id = 1

    async def download(document, destination):
        destination.write(b"file content")

    calls.bot.download = AsyncMock(side_effect=download)
    yield calls
    imports.router._parent_router = None
    dp.sub_routers.clear()


async def _feed(telegram, update: Update) -> None:
    await telegram.dp.feed_update(telegram.bot, update, import_service=telegram.service)


@pytest.mark.parametrize("chat_id", [ADMIN, GROUP])
async def test_admin_can_send_a_file_in_private_and_in_a_group(telegram, chat_id: int) -> None:
    await _feed(telegram, _document_update(ADMIN, chat_id, "leads_1.JSON"))

    telegram.service.import_bytes.assert_awaited_once_with(
        b"file content", "leads_1.JSON", SOURCE_TELEGRAM, exclude_chat=chat_id
    )
    assert "Импорт завершён" in telegram.status.edit_text.await_args.args[0]


@pytest.mark.parametrize("chat_id", [STRANGER, GROUP])
async def test_file_from_a_stranger_is_not_even_downloaded(telegram, chat_id: int) -> None:
    await _feed(telegram, _document_update(STRANGER, chat_id, "leads_1.json"))
    telegram.bot.download.assert_not_awaited()
    telegram.service.import_bytes.assert_not_awaited()
    telegram.reply.assert_not_awaited()  # a refusal to every file in a group would be spam
    telegram.answer.assert_not_awaited()


@pytest.mark.parametrize("file_name", ["leads.csv", "leads.json.exe", "leads", None])
async def test_only_json_files_are_taken(telegram, file_name: str | None) -> None:
    await _feed(telegram, _document_update(ADMIN, ADMIN, file_name))
    telegram.bot.download.assert_not_awaited()
    telegram.service.import_bytes.assert_not_awaited()


async def test_too_big_file_is_refused_before_downloading(telegram) -> None:
    await _feed(telegram, _document_update(ADMIN, ADMIN, "leads.json", file_size=MAX_BYTES + 1))
    telegram.bot.download.assert_not_awaited()
    telegram.service.import_bytes.assert_not_awaited()
    assert "больше допустимых" in telegram.answer.await_args.args[0]


async def test_failed_download_is_reported_not_raised(telegram) -> None:
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    telegram.bot.download = AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(spec=SendMessage), message="x"))
    await _feed(telegram, _document_update(ADMIN, ADMIN, "leads.json"))
    telegram.service.import_bytes.assert_not_awaited()
    assert "Не удалось скачать" in telegram.status.edit_text.await_args.args[0]


async def test_real_service_through_the_handler(telegram, session_maker, tmp_path: Path) -> None:
    report = AsyncMock()
    telegram.service = ImportService(session_maker, report, tmp_path, MAX_BYTES, MAX_LEADS)
    data = _file([_record(1)])

    async def download(document, destination):
        destination.write(data)

    telegram.bot.download = AsyncMock(side_effect=download)
    await _feed(telegram, _document_update(ADMIN, GROUP, "leads_1.json"))

    assert len(await _leads(session_maker)) == 1
    assert report.await_args.args[1] == GROUP  # the group sees the edited note, others a notification
    async with session_maker() as session:
        total = (await session.execute(select(func.count(ImportLog.id)))).scalar_one()
    assert total == 1
