"""Tests for choosing the niches to check first, /niches, and the useful leads in the report on reading cards."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select

from src.bot.formatters import (
    MAX_MESSAGE_LENGTH,
    format_import_messages,
    format_issued_lead,
    format_niche_table,
    split_messages,
)
from src.bot.handlers import imports
from src.bot.keyboards import FIND_IN_2GIS_BUTTON_TEXT, OPEN_CARD_BUTTON_TEXT, get_issued_lead_keyboard
from src.bot.notifier import AdminNotifier
from src.database.models import STATUS_ISSUED, STATUS_NEW, Lead
from src.database.repo import get_niche_site_counts, issue_next_leads
from src.services import lead_enrich
from src.services.lead_enrich import UsefulLead
from src.services.lead_file import KIND_ENRICH_RESULT, build_enrich_document, enrich_result_record
from src.services.lead_import import ImportResult, ImportService
from src.services.niche_priority import (
    EXPLORE_CHECKED_BELOW,
    MAX_LEADS_PER_NICHE,
    NicheStats,
    choose_export,
    load_niche_stats,
    plan_export,
    resolve_niche,
    stats_from_counts,
    useful_share,
)
from src.services.site_status import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNCERTAIN,
    SITE_UNKNOWN,
)
from src.services.twogis_links import twogis_card_url, twogis_search_url

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
_numbers = iter(range(1000, 10**6))


def _lead(category: str = "beauty", status: str = SITE_UNKNOWN, **overrides) -> Lead:
    number = next(_numbers)
    values = {
        "external_id": f"2gis_7{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": category,
        "address": f"Невский проспект, {number}",
        "site_status": status,
        "score": 40,
        "crm_status": STATUS_NEW,
        "source_url": f"https://2gis.ru/spb/firm/7{number}",
        "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(seconds=number),
    }
    values.update(overrides)
    return Lead(**values)


def _unknown(category: str, count: int) -> list[Lead]:
    return [_lead(category) for _ in range(count)]


def _checked(category: str, no_site: int = 0, dead: int = 0, has_site: int = 0) -> list[Lead]:
    return [
        *(_lead(category, SITE_NO_SITE, site_confidence=CONFIDENCE_MEDIUM) for _ in range(no_site)),
        *(_lead(category, SITE_DEAD) for _ in range(dead)),
        *(_lead(category, SITE_FOUND) for _ in range(has_site)),
    ]


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


def _niches_of(leads: list[Lead]) -> Counter[str]:
    return Counter(lead.category for lead in leads)


# ---------- the share ----------


@pytest.mark.parametrize(
    ("checked", "no_site", "dead", "expected"),
    [
        (0, 0, 0, 0.5),  # nobody has checked: in the middle, not at the bottom
        (35, 1, 0, 2 / 37),
        (35, 0, 0, 1 / 37),
        (4, 2, 2, 5 / 6),
        (10, 5, 0, 0.5),
    ],
)
def test_the_share_is_smoothed(checked: int, no_site: int, dead: int, expected: float) -> None:
    assert useful_share(checked, no_site, dead) == pytest.approx(expected)


def test_an_empty_niche_gets_a_half() -> None:
    assert NicheStats(category="x").share == 0.5
    assert NicheStats(category="x", unknown=50).share == 0.5  # unknown leads are not checked ones


def test_statistics_count_each_status_in_its_column() -> None:
    counts = {
        "a": {
            SITE_NO_SITE: 1,
            SITE_NOT_FOUND: 2,
            SITE_SOCIAL_ONLY: 3,
            SITE_DEAD: 4,
            SITE_FOUND: 5,
            SITE_UNKNOWN: 6,
            SITE_UNCERTAIN: 7,
        }
    }

    item = stats_from_counts(counts)[0]

    assert item.no_site == 6
    assert item.dead == 4
    assert item.has_site == 5
    assert item.unknown == 6
    assert item.checked == 15  # not the unknown, not the uncertain


async def test_statistics_from_the_database(session_maker) -> None:
    await _add(
        session_maker,
        *_checked("a", no_site=1, dead=1, has_site=2),
        *_unknown("a", 3),
        _lead("a", SITE_UNKNOWN, crm_status=STATUS_ISSUED),  # not left to check
        _lead("a", SITE_NO_SITE, crm_status=STATUS_ISSUED),  # checked, whatever became of it
        _lead("a", SITE_NO_SITE, site_confidence=CONFIDENCE_LOW),  # only an empty tag in OSM
        _lead("a", SITE_NO_SITE, is_chain=True),
        _lead("a", SITE_UNKNOWN, is_chain=True),
        *_unknown("b", 1),
    )

    async with session_maker() as session:
        stats = {item.category: item for item in await load_niche_stats(session)}

    assert stats["a"].no_site == 2
    assert stats["a"].dead == 1
    assert stats["a"].has_site == 2
    assert stats["a"].unknown == 3
    assert stats["b"].checked == 0
    assert stats["b"].unknown == 1


async def test_counts_use_only_what_exists(session_maker) -> None:
    async with session_maker() as session:
        assert await get_niche_site_counts(session) == {}


# ---------- the order of a file ----------


def _stats(category: str, no_site: int = 0, has_site: int = 0) -> NicheStats:
    return NicheStats(category=category, no_site=no_site, has_site=has_site)


def test_niches_go_by_their_share() -> None:
    stats = [_stats("low", no_site=1, has_site=30), _stats("high", no_site=20, has_site=10)]
    candidates = [*_unknown("low", 5), *_unknown("high", 5)]

    plan = plan_export(candidates, stats, 8)

    assert [lead.category for lead in plan.leads] == ["high"] * 5 + ["low"] * 3
    assert plan.explored == 0


def test_no_more_than_ten_of_a_niche() -> None:
    plan = plan_export([*_unknown("a", 30), *_unknown("b", 30)], [_stats("a", 9, 9), _stats("b", 1, 9)], 30)

    assert _niches_of(plan.leads) == {"a": MAX_LEADS_PER_NICHE, "b": MAX_LEADS_PER_NICHE}


def test_one_niche_cannot_fill_a_file_alone() -> None:
    plan = plan_export(_unknown("a", 50), [_stats("a", 9, 9)], 30)

    assert len(plan.leads) == MAX_LEADS_PER_NICHE


def test_the_order_within_a_niche_is_kept() -> None:
    candidates = _unknown("a", 12)

    plan = plan_export(candidates, [_stats("a", 9, 9)], 5)

    assert plan.leads == candidates[:5]


def test_a_fifth_of_a_file_goes_to_niches_hardly_tried() -> None:
    # The checked niche is much better, and would take everything if not for exploring.
    stats = [_stats("good", no_site=90, has_site=10), _stats("new1", has_site=1), _stats("new2", has_site=1)]
    candidates = [*_unknown("good", 10), *_unknown("other", 0), *_unknown("new1", 10), *_unknown("new2", 10)]

    plan = plan_export(candidates, stats, 10)

    new = sum(1 for lead in plan.leads if lead.category != "good")
    assert new >= 2
    assert plan.explored == 2
    assert _niches_of(plan.leads[:2]) == {"new1": 1, "new2": 1}  # evenly, and first


def test_exploring_comes_first_because_a_run_reads_only_thirty_cards() -> None:
    stats = [
        *(_stats(f"good{number}", no_site=90, has_site=10) for number in range(6)),
        _stats("new", has_site=EXPLORE_CHECKED_BELOW - 1),
        _stats("new2", has_site=0),
    ]
    candidates = [lead for item in stats for lead in _unknown(item.category, 20)]

    plan = plan_export(candidates, stats, 60)

    first_thirty = plan.leads[:30]
    explored = [lead for lead in first_thirty if lead.category.startswith("new")]
    assert len(explored) >= 6
    assert len(plan.leads) == 60
    assert max(_niches_of(plan.leads).values()) <= MAX_LEADS_PER_NICHE


def test_a_niche_with_five_checked_is_not_exploring_any_more() -> None:
    stats = [_stats("good", 90, 10), _stats("tried", no_site=0, has_site=EXPLORE_CHECKED_BELOW)]

    plan = plan_export([*_unknown("good", 10), *_unknown("tried", 10)], stats, 10)

    assert plan.explored == 0
    assert _niches_of(plan.leads) == {"good": 10}


def test_a_niche_nobody_has_checked_gets_into_a_file() -> None:
    plan = plan_export([*_unknown("good", 10), *_unknown("unseen", 10)], [_stats("good", 90, 10)], 10)

    assert _niches_of(plan.leads)["unseen"] >= 2


def test_without_niches_to_explore_the_whole_file_goes_by_share() -> None:
    plan = plan_export(_unknown("a", 5), [_stats("a", 9, 9)], 5)

    assert plan.explored == 0
    assert len(plan.leads) == 5


def test_exploring_niches_short_of_leads_leave_the_place_to_others() -> None:
    plan = plan_export([*_unknown("good", 10), *_unknown("new", 1)], [_stats("good", 90, 10)], 10)

    assert plan.explored == 1
    assert _niches_of(plan.leads) == {"new": 1, "good": 9}


def test_a_small_file_still_explores() -> None:
    plan = plan_export([*_unknown("good", 5), *_unknown("new", 5)], [_stats("good", 90, 10)], 1)

    assert [lead.category for lead in plan.leads] == ["new"]


def test_nothing_to_export() -> None:
    assert plan_export([], [], 30).leads == []


async def test_only_the_pool_gets_into_a_file(session_maker) -> None:
    await _add(
        session_maker,
        _lead("a", name="подходит"),
        _lead("a", is_chain=True),
        _lead("a", crm_status=STATUS_ISSUED),
        _lead("a", SITE_NO_SITE),
        _lead("a", source_url=None),
        _lead("a", source="osm", external_id="osm_1"),
    )

    async with session_maker() as session:
        plan = await choose_export(session, 30)

    assert [lead.name for lead in plan.leads] == ["подходит"]


async def test_a_named_niche_gives_only_it_and_without_the_cap(session_maker) -> None:
    await _add(session_maker, *_unknown("a", 15), *_unknown("b", 15))

    async with session_maker() as session:
        plan = await choose_export(session, 12, "b")

    assert _niches_of(plan.leads) == {"b": 12}
    assert plan.explored == 0


async def test_the_order_comes_from_checked_leads_in_the_database(session_maker) -> None:
    await _add(
        session_maker,
        *_checked("bad", no_site=1, has_site=34),
        *_checked("fine", no_site=6, dead=2, has_site=2),
        *_unknown("bad", 5),
        *_unknown("fine", 5),
    )

    async with session_maker() as session:
        plan = await choose_export(session, 7)

    assert [lead.category for lead in plan.leads] == ["fine"] * 5 + ["bad"] * 2


# ---------- finding a niche by name ----------


@pytest.mark.parametrize("text", ["beauty", "BEAUTY", " beauty ", "💇‍♀️ Салоны красоты и косметология"])
def test_a_niche_is_found_by_its_key_or_title(text: str) -> None:
    assert resolve_niche(text, ["dentist_clinic", "beauty"]) == "beauty"


def test_a_niche_is_found_by_a_title_without_the_picture() -> None:
    assert resolve_niche("салоны красоты и косметология", ["beauty"]) == "beauty"


def test_a_free_text_niche_from_the_database_is_found() -> None:
    assert resolve_niche("Ремонт обуви", ["beauty", "ремонт обуви"]) == "ремонт обуви"


def test_an_unknown_niche_is_not_found() -> None:
    assert resolve_niche("космодром", ["beauty"]) is None


# ---------- /export_unknown ----------


async def _export(session_maker, monkeypatch, args: str | None) -> MagicMock:
    monkeypatch.setattr(imports, "async_session_maker", session_maker)
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_document = AsyncMock()
    await imports.handle_export_unknown(message, MagicMock(args=args))
    return message


def _sent(message: MagicMock) -> dict:
    return json.loads(message.answer_document.await_args.args[0].data.decode("utf-8"))


async def test_the_command_with_a_number_and_a_niche(session_maker, monkeypatch) -> None:
    beauty = _unknown("beauty", 5)
    await _add(session_maker, *beauty, *_unknown("dentist_clinic", 5))

    message = await _export(session_maker, monkeypatch, "3 beauty")

    document = _sent(message)
    assert document["count"] == 3
    assert {record["name"] for record in document["records"]} <= {lead.name for lead in beauty}
    assert "ниша" in message.answer_document.await_args.kwargs["caption"]


async def test_the_command_with_only_a_niche_takes_the_default_number(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_unknown("beauty", 40), *_unknown("dentist_clinic", 5))

    message = await _export(session_maker, monkeypatch, "beauty")

    assert _sent(message)["count"] == 30


async def test_the_command_finds_a_niche_of_several_words(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_unknown("ремонт обуви", 2))

    message = await _export(session_maker, monkeypatch, "5 Ремонт обуви")

    assert _sent(message)["count"] == 2


async def test_an_unknown_niche_gets_a_list_of_niches(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_unknown("свой <запрос>", 1))

    message = await _export(session_maker, monkeypatch, "5 <b>космодром")

    message.answer_document.assert_not_awaited()
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "Неизвестная ниша: &lt;b&gt;космодром" in text
    assert "beauty" in text
    assert "dentist_clinic" in text
    assert "свой &lt;запрос&gt;" in text
    assert all(len(call.args[0]) <= MAX_MESSAGE_LENGTH for call in message.answer.await_args_list)


async def test_a_known_niche_with_nothing_left_says_so(session_maker, monkeypatch) -> None:
    message = await _export(session_maker, monkeypatch, "beauty")

    message.answer_document.assert_not_awaited()
    assert "в нише" in message.answer.await_args.args[0]


async def test_the_caption_explains_a_short_file(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_unknown("beauty", 20))

    message = await _export(session_maker, monkeypatch, "30")

    assert _sent(message)["count"] == MAX_LEADS_PER_NICHE
    assert "не больше 10" in message.answer_document.await_args.kwargs["caption"]


async def test_the_file_still_has_only_the_agreed_fields(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_unknown("beauty", 2))

    message = await _export(session_maker, monkeypatch, None)

    assert set(_sent(message)["records"][0]) == {"source", "source_id", "source_url", "name", "district"}


# ---------- /niches ----------


def test_the_table_has_all_the_columns_sorted_by_share() -> None:
    stats = stats_from_counts(
        {
            "beauty": {SITE_NO_SITE: 1, SITE_FOUND: 34, SITE_UNKNOWN: 7},
            "hotels": {SITE_NO_SITE: 6, SITE_DEAD: 2, SITE_FOUND: 2, SITE_UNKNOWN: 3},
            "massage": {SITE_UNKNOWN: 9},
        }
    )

    text = "\n".join(format_niche_table(stats))

    assert text.index("Отели") < text.index("Массаж") < text.index("Салоны")
    row = next(line for line in text.splitlines() if "Отели" in line)
    assert row.split()[:6] == ["75%", "10", "6", "2", "2", "3"]
    row = next(line for line in text.splitlines() if "Массаж" in line)
    assert row.split()[:6] == ["50%", "0", "0", "0", "0", "9"]


def test_a_long_table_is_cut_into_whole_tables() -> None:
    stats = [NicheStats(category=f"ниша <{number}> " + "x" * 40, unknown=number) for number in range(400)]

    messages = format_niche_table(stats)

    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_LENGTH for message in messages)
    tables = [message for message in messages if "<pre>" in message]
    assert all(message.count("<pre>") == message.count("</pre>") for message in tables)
    assert sum(message.count("&lt;") for message in messages) == 400


def test_an_empty_table_says_so() -> None:
    assert format_niche_table([]) == ["Лидов пока нет, считать нечего."]


async def test_the_command_shows_the_table(session_maker, monkeypatch) -> None:
    await _add(session_maker, *_checked("beauty", no_site=1), *_unknown("beauty", 2))
    monkeypatch.setattr(imports, "async_session_maker", session_maker)
    message = MagicMock()
    message.answer = AsyncMock()

    await imports.handle_niches(message)

    assert "Салоны" in message.answer.await_args.args[0]


# ---------- the card of a handed-out lead ----------


def _button_urls(lead: Lead) -> dict[str, str]:
    keyboard = get_issued_lead_keyboard(lead)
    return {button.text: button.url for row in keyboard.inline_keyboard for button in row if button.url}


@pytest.mark.parametrize("status", [SITE_NO_SITE, SITE_DEAD, SITE_SOCIAL_ONLY, SITE_NOT_FOUND])
def test_a_card_with_a_link_to_2gis_opens_it(status: str) -> None:
    lead = _lead(status=status, id=1)

    urls = _button_urls(lead)

    assert urls[OPEN_CARD_BUTTON_TEXT] == lead.source_url
    assert FIND_IN_2GIS_BUTTON_TEXT not in urls


@pytest.mark.parametrize("source_url", [None, "", "https://www.openstreetmap.org/node/1"])
@pytest.mark.parametrize("status", [SITE_NO_SITE, SITE_DEAD])
def test_a_card_without_a_link_gets_a_search_in_2gis(status: str, source_url: str | None) -> None:
    lead = _lead(status=status, id=1, source_url=source_url, name="Улыбка", address="Невский, 1")

    urls = _button_urls(lead)

    assert OPEN_CARD_BUTTON_TEXT not in urls
    assert urls[FIND_IN_2GIS_BUTTON_TEXT] == twogis_search_url("Улыбка", "Невский, 1")


def test_a_lead_with_an_unknown_site_gets_no_search_button() -> None:
    assert FIND_IN_2GIS_BUTTON_TEXT not in _button_urls(_lead(status=SITE_UNKNOWN, id=1, source_url=None))


def test_a_search_link_is_fit_for_a_button_whatever_the_name() -> None:
    url = twogis_search_url("Очень длинное название организации " * 20, "Адрес / с <символами> & прочим " * 20)

    assert url.startswith("https://2gis.ru/spb/search/")
    assert twogis_card_url(url) == url  # checked host, no spaces, within the limit of a button
    assert " " not in url
    assert "<" not in url


def test_a_search_does_not_need_an_address() -> None:
    assert twogis_search_url("Улыбка", None) == twogis_search_url("Улыбка", "")


def test_a_bad_city_does_not_get_into_a_link() -> None:
    assert twogis_search_url("Улыбка", None, "../evil").startswith("https://2gis.ru/spb/search/")


def test_the_text_of_a_card_stays_as_it_was() -> None:
    text = format_issued_lead(_lead(status=SITE_NO_SITE, id=1))

    assert "Найти в 2ГИС" not in text


# ---------- the report on reading cards ----------


def _useful(status: str = SITE_NO_SITE, **overrides) -> UsefulLead:
    values = {
        "name": "Улыбка",
        "category": "dentist_clinic",
        "site_status": status,
        "address": "Невский проспект, 10",
        "phone": "+79210000001",
        "source_url": "https://2gis.ru/spb/firm/70001",
    }
    values.update(overrides)
    return UsefulLead(**values)


def _report(*useful: UsefulLead) -> list[str]:
    stats = lead_enrich.EnrichStats(updated=len(useful), useful=list(useful))
    return format_import_messages(
        ImportResult(source="telegram", file_name="enrich.json", total=len(useful), enrichment=stats, pool_size=3)
    )


def test_the_report_lists_useful_leads_with_links() -> None:
    text = "\n\n".join(_report(_useful()))

    assert "Карточки 2ГИС обработаны" in text
    assert "Сайта нет: 1" in text
    assert "Улыбка" in text
    assert "Стоматологии" in text
    assert "Невский проспект, 10" in text
    assert "+79210000001" in text
    assert '<a href="https://2gis.ru/spb/firm/70001">Открыть в 2ГИС</a>' in text


def test_first_without_a_site_then_with_a_dead_one() -> None:
    text = "\n\n".join(
        _report(
            _useful(SITE_DEAD, name="Мёртвый", website="https://dead.example"),
            _useful(name="Нет его"),
        )
    )

    assert text.index("Сайта нет: 1") < text.index("Нет его") < text.index("Сайт не открывается: 1")
    assert text.index("Сайт не открывается: 1") < text.index("Мёртвый")
    assert "https://dead.example" in text


def test_a_lead_without_a_card_gets_a_search_in_2gis() -> None:
    text = "\n".join(_report(_useful(source_url=None, name="Улыбка", address="Невский, 1")))

    assert "Найти в 2ГИС" in text
    assert twogis_search_url("Улыбка", "Невский, 1") in text


def test_a_link_to_another_site_is_not_called_a_link_to_2gis() -> None:
    text = "\n".join(_report(_useful(source_url="https://www.openstreetmap.org/node/1")))

    assert "openstreetmap" not in text
    assert "Найти в 2ГИС" in text


def test_a_lead_without_a_phone_or_an_address_has_no_empty_lines() -> None:
    text = "\n".join(_report(_useful(phone=None, address=None)))

    assert "📞" not in text
    assert "📫" not in text


def test_everything_from_a_file_is_escaped() -> None:
    text = "\n".join(
        _report(
            _useful(
                SITE_DEAD,
                name="<b>Улыбка</b> & Ко",
                address="<script>",
                phone="<1>",
                website='https://x.example/?a="1"&b=<2>',
                source_url=None,
            )
        )
    )

    assert "<script>" not in text
    assert "&lt;b&gt;Улыбка&lt;/b&gt; &amp; Ко" in text
    assert "&lt;1&gt;" in text
    assert "&lt;2&gt;" in text


def test_no_useful_leads_gives_the_line() -> None:
    assert "Полезных лидов в этом файле нет" in "\n".join(_report())


def test_a_long_list_goes_into_several_whole_messages() -> None:
    useful = [_useful(name=f"Организация {number}", address="Адрес " * 30) for number in range(100)]

    messages = _report(*useful)

    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_LENGTH for message in messages)
    assert "Карточки 2ГИС обработаны" in messages[0]
    text = "\n".join(messages)
    assert all(f"Организация {number}<" in text for number in range(100))
    for message in messages:
        assert message.count("<a ") == message.count("</a>")
        assert message.count("<b>") == message.count("</b>")


def test_split_keeps_order_and_blocks_whole() -> None:
    assert split_messages(["a", "b"]) == ["a\n\nb"]
    long_block = "x" * 3000

    assert split_messages([long_block, long_block, "c"]) == [long_block, f"{long_block}\n\nc"]
    assert split_messages([]) == []


def test_a_failed_import_stays_one_message() -> None:
    messages = format_import_messages(ImportResult(source="telegram", file_name="a.json", ok=False, error="сломан"))

    assert len(messages) == 1
    assert "сломан" in messages[0]


async def test_admins_get_every_message_without_previews() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    notifier = AdminNotifier(bot, frozenset({100}), (), True)
    useful = [_useful(name=f"Организация {number}", address="Адрес " * 30) for number in range(100)]
    stats = lead_enrich.EnrichStats(updated=100, useful=useful)

    await notifier.report_import(ImportResult(source="inbox", file_name="e.json", total=100, enrichment=stats))

    assert bot.send_message.await_count > 1
    calls = bot.send_message.await_args_list
    assert "Карточки 2ГИС обработаны" in calls[0].args[1]
    assert all(call.kwargs["link_preview_options"].is_disabled for call in calls)


async def test_the_chat_that_sent_the_file_gets_every_message(monkeypatch) -> None:
    monkeypatch.setattr(Message, "answer", AsyncMock())
    message = MagicMock()
    message.answer = AsyncMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    message.answer.return_value = status
    message.document = MagicMock(file_size=10, file_name="enrich.json")
    bot = MagicMock()
    bot.download = AsyncMock()
    useful = [_useful(name=f"Организация {number}", address="Адрес " * 30) for number in range(100)]
    result = ImportResult(source="telegram", file_name="e.json", total=100)
    result.enrichment = lead_enrich.EnrichStats(updated=100, useful=useful)
    service = MagicMock(max_bytes=1000)
    service.import_bytes = AsyncMock(return_value=result)

    await imports.handle_lead_file(message, bot, service)

    assert "Карточки 2ГИС обработаны" in status.edit_text.await_args.args[0]
    assert message.answer.await_count > 1  # the note, then the rest


# ---------- from a file to the report ----------


@pytest.fixture
def service(session_maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportService:
    async def analyze(url, client=None):
        if "dead" in url:
            return SITE_DEAD, "HTTP/Connection error"
        if "vk.com" in url:
            return SITE_SOCIAL_ONLY, url
        return "has_site", url

    monkeypatch.setattr(lead_enrich, "analyze_website", analyze)
    return ImportService(session_maker, AsyncMock(), tmp_path / "inbox", 1024 * 1024, 100)


def _result_file(*records: dict) -> bytes:
    document = build_enrich_document(KIND_ENRICH_RESULT, list(records), NOW)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _record(lead: Lead, **fields) -> dict:
    return enrich_result_record(
        lead.source, lead.external_id.removeprefix(f"{lead.source}_"), datetime.now(UTC), parsed=True, **fields
    )


async def test_a_file_with_useful_leads_lists_them_and_leaves_them_in_the_pool(session_maker, service) -> None:
    no_site, dead, social, working = (_lead(name=name) for name in ("Без сайта", "Мёртвый", "Соцсеть", "Рабочий"))
    await _add(session_maker, no_site, dead, social, working)

    result = await service.import_bytes(
        _result_file(
            _record(no_site, phones=["+79210000001"]),
            _record(dead, website="https://dead.example"),
            _record(social, website="https://vk.com/x"),
            _record(working, website="https://working.example"),
        ),
        "enrich.json",
        "telegram",
    )

    text = "\n\n".join(format_import_messages(result))
    assert "Без сайта" in text
    assert "Мёртвый" in text
    assert "https://dead.example" in text
    assert "только соцсеть" in text
    assert "Рабочий" not in text
    assert f'href="{no_site.source_url}"' in text
    assert "+79210000001" in text
    assert "Сайта нет: 2" in text

    # They stay in the pool and go out with the next batch as usual.
    async with session_maker() as session:
        leads = (await session.scalars(select(Lead).where(Lead.name != "Рабочий"))).all()
        assert all(lead.crm_status == STATUS_NEW and lead.issued_at is None for lead in leads)
        issued = await issue_next_leads(session, 10)
    assert {lead.name for lead in issued} == {"Без сайта", "Мёртвый", "Соцсеть"}


async def test_a_file_without_useful_leads_says_so(session_maker, service) -> None:
    working = _lead()
    await _add(session_maker, working)

    result = await service.import_bytes(
        _result_file(_record(working, website="https://working.example")), "enrich.json", "telegram"
    )

    assert "Полезных лидов в этом файле нет" in "\n".join(format_import_messages(result))


async def test_ignored_records_and_chains_are_not_listed(session_maker, service) -> None:
    issued = _lead(name="Выдан", crm_status=STATUS_ISSUED)
    chain = _lead(name="Сеть", is_chain=True)
    await _add(session_maker, issued, chain)

    result = await service.import_bytes(_result_file(_record(issued), _record(chain)), "enrich.json", "telegram")

    assert "Полезных лидов в этом файле нет" in "\n".join(format_import_messages(result))


async def test_checked_leads_change_the_order_of_the_next_file(session_maker, service) -> None:
    await _add(session_maker, *_checked("good", no_site=8, has_site=2), *_unknown("good", 2), *_unknown("poor", 2))
    poor_ones = [_lead("poor") for _ in range(12)]
    await _add(session_maker, *poor_ones)

    await service.import_bytes(
        _result_file(*(_record(lead, website="https://working.example") for lead in poor_ones)),
        "enrich.json",
        "telegram",
    )

    async with session_maker() as session:
        plan = await choose_export(session, 3)
    assert [lead.category for lead in plan.leads] == ["good", "good", "poor"]
