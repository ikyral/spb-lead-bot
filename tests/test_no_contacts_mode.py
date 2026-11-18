"""Leads without contacts: a link to the 2GIS card instead of a phone, and remembering how far a source was read."""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, ForceReply, Message, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.bot import formatters
from src.bot.formatters import format_collector_status, format_issued_lead, format_lead_card, generate_pitch_text
from src.bot.handlers import issue
from src.bot.keyboards import (
    ADD_PHONE_BUTTON_TEXT,
    OPEN_CARD_BUTTON_TEXT,
    get_issued_lead_keyboard,
    get_lead_card_keyboard,
)
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import Settings
from src.database.models import STATUS_ISSUED, STATUS_NEW, Lead, ProviderState, SearchProgress
from src.database.repo import (
    count_new_leads,
    count_new_leads_with_contacts,
    enable_provider,
    get_provider_states,
    init_db,
    issue_next_leads,
    save_new_leads,
)
from src.services import pipeline
from src.services.api_client import ProviderAuthError, ProviderTemporaryError
from src.services.collector import MAX_CONSECUTIVE_BLOCKS, CollectorConfig, collect_leads, load_collector_config
from src.services.dedup import merge_duplicate_places
from src.services.lead_export import export_new_leads
from src.services.lead_file import REASON_NO_CONTACTS, build_document, lead_to_record, parse_lead_file
from src.services.lead_import import SOURCE_TELEGRAM, ImportService
from src.services.normalize import parse_manual_phone
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.registry import ProviderSet
from src.services.providers.twogis_catalog import parse_catalog_items
from src.services.scheduler import CollectorRunner, RunReport
from src.services.scorer import SITE_UNKNOWN, calculate_lead_score
from src.services.twogis_links import build_card_url, twogis_card_url

ADMIN = 111
STRANGER = 999
GROUP = -100500
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _items(count: int, first: int = 1, total: int | None = 1000) -> dict:
    items = [
        {
            "id": f"{number}_hash{number}",
            "type": "branch",
            "name": f"Организация {number}",
            "address_name": f"Невский проспект, {number}",
            "point": {"lat": 59.9, "lon": 30.3 + number / 10000},
            "rubrics": [
                {"name": "Дополнительная", "kind": "additional"},
                {"name": "Стоматологические клиники", "kind": "primary"},
            ],
        }
        for number in range(first, first + count)
    ]
    return {"meta": {"code": 200}, "result": {"total": total, "items": items}}


def _answer(payload: dict):
    return lambda request: httpx.Response(200, json=payload)


def _config(**overrides) -> CollectorConfig:
    values = {"COLLECT_QUERIES": "dentist_clinic", "COLLECT_DISTRICTS": "Центральный"} | overrides
    return load_collector_config(Settings(_env_file=None, **values))


def _place(number: int = 1, **overrides) -> RawPlace:
    values = {
        "external_id": f"2gis_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "address": f"Невский проспект, {number}",
        "source_url": f"https://2gis.ru/spb/firm/{number}",
        "contacts_known": False,
    } | overrides
    return RawPlace(**values)


def _lead(number: int = 1, **overrides) -> Lead:
    values = {
        "id": number,
        "external_id": f"2gis_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "address": "Невский проспект, 10",
        "site_status": SITE_UNKNOWN,
        "score": 45,
        "crm_status": STATUS_ISSUED,
        "source_url": f"https://2gis.ru/spb/firm/{number}",
    } | overrides
    return Lead(**values)


async def _saved(session_maker) -> list[Lead]:
    async with session_maker() as session:
        return list((await session.scalars(select(Lead).order_by(Lead.id))).all())


def _buttons(lead: Lead) -> list[list]:
    return get_issued_lead_keyboard(lead).inline_keyboard


# ---------- quality filter ----------


async def test_without_contacts_a_2gis_organization_is_a_lead_by_default(session_maker) -> None:
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [_place()])
    assert (stats["qualified"], stats["saved_new"]) == (1, 1)

    lead = (await _saved(session_maker))[0]
    assert lead.crm_status == STATUS_NEW
    assert lead.source_url == "https://2gis.ru/spb/firm/1"
    # Not 'no_site': nobody knows whether it has a site, and the pitch must not claim it.
    assert lead.site_status == SITE_UNKNOWN
    # "📋 Новые лиды" shows leads from 30 points, the niche must not change that.
    assert lead.score >= 30


async def test_low_margin_niche_also_qualifies_and_is_listed(session_maker) -> None:
    assert calculate_lead_score("beauty", SITE_UNKNOWN, None, None) == 40
    assert calculate_lead_score("horeca", SITE_UNKNOWN, None, None) == 20
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [_place(category="horeca")])
    assert len(await _saved(session_maker)) == 1


async def test_required_contacts_bring_the_old_behaviour_back(session_maker, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.settings, "LEADS_REQUIRE_CONTACT", True)
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [_place()])
    assert (stats["qualified"], stats["saved_new"]) == (0, 0)
    assert await _saved(session_maker) == []


@pytest.mark.parametrize("require_contact", [False, True])
async def test_lead_with_a_phone_passes_either_way(session_maker, monkeypatch, require_contact: bool) -> None:
    monkeypatch.setattr(pipeline.settings, "LEADS_REQUIRE_CONTACT", require_contact)
    place = _place(phone="+78125550101", contacts_known=True)
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [place])
    lead = (await _saved(session_maker))[0]
    assert (lead.phone, lead.site_status) == ("+78125550101", "no_site")


async def test_source_that_reports_no_contacts_is_not_affected(session_maker) -> None:
    """OSM has no card with contacts behind it: there would be nothing for the owner to open."""
    place = _place(source="osm", external_id="osm_1", contacts_known=True, source_url="https://osm.org/node/1")
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [place])
    assert stats["saved_new"] == 0


async def test_site_check_is_not_run_for_a_lead_without_contacts(session_maker, monkeypatch) -> None:
    checked: list[object] = []

    async def fake_analyze(url, client=None):
        checked.append(url)
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [_place()])
    assert checked == [None]  # the function returns at once for an empty address, nothing goes out


# ---------- duplicates without a phone ----------


async def test_same_source_id_is_saved_once(session_maker) -> None:
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [_place()])
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [_place()])
    assert (stats["saved_new"], stats["duplicates_skipped"]) == (0, 1)
    assert len(await _saved(session_maker)) == 1


async def test_same_name_and_address_is_one_organization(session_maker) -> None:
    same = _place(2, name="организация 1, стоматология", address="НЕВСКИЙ проспект, 1")
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [_place(1), same])
    assert stats["saved_new"] == 1

    async with session_maker() as session:
        stats = await pipeline.save_collected_places(
            session, [_place(3, name="Организация 1", address="Невский проспект, 1")]
        )
    assert stats["saved_new"] == 0


async def test_branches_at_different_addresses_stay_separate(session_maker) -> None:
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [_place(1), _place(2, name="Организация 1")])
    assert stats["saved_new"] == 2


async def test_phone_still_finds_duplicates(session_maker) -> None:
    places = [
        _place(1, phone="+7 812 555-01-01", contacts_known=True),
        _place(2, name="Другое имя", address="Другой адрес", phone="8 812 5550101", contacts_known=True),
    ]
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, places)
    assert stats["saved_new"] == 1


def test_merged_place_keeps_the_2gis_card_and_takes_contacts_from_the_other_source() -> None:
    osm = _place(1, source="osm", external_id="osm_1", source_url="https://osm.org/node/1", contacts_known=True)
    twogis_place = _place(2, name="Организация 1", address="Невский проспект, 1", rubric="Клиника")
    merged = merge_duplicate_places([osm, twogis_place])
    assert len(merged) == 1
    assert merged[0].source_url == "https://2gis.ru/spb/firm/2"
    assert merged[0].rubric == "Клиника"
    assert merged[0].contacts_known is False


# ---------- card links ----------


@pytest.mark.parametrize(
    ("city", "firm_id", "expected"),
    [
        ("spb", "70000001018420390_abc123", "https://2gis.ru/spb/firm/70000001018420390"),
        ("spb", "70000001018420390", "https://2gis.ru/spb/firm/70000001018420390"),
        ("spb", 141265769336625, "https://2gis.ru/spb/firm/141265769336625"),
        ("spb", "abc_123", None),
        ("spb", "", None),
        ("spb", None, None),
        ("spb", "12/../../evil", None),
        ("Санкт-Петербург", "1", None),
        ("../evil", "1", None),
    ],
)
def test_card_url(city: str, firm_id: object, expected: str | None) -> None:
    assert build_card_url(city, firm_id) == expected


def test_lead_without_a_buildable_link_is_kept_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    data = {"result": {"items": [{"id": "не-число", "type": "branch", "name": "Странная"}]}}
    with caplog.at_level(logging.WARNING):
        places = parse_catalog_items(data, None, "beauty", True)
    assert [(place.name, place.source_url) for place in places] == [("Странная", None)]
    assert "card link" in caplog.text


def test_provider_uses_the_configured_city() -> None:
    places = parse_catalog_items(_items(1), None, "beauty", True, city_slug="moscow")
    assert places[0].source_url == "https://2gis.ru/moscow/firm/1"


@pytest.mark.parametrize(
    ("url", "usable"),
    [
        ("https://2gis.ru/spb/firm/1", True),
        ("https://2gis.ru/firm/1", True),  # leads saved by earlier versions
        ("https://www.2gis.kz/almaty/firm/1", True),
        ("http://2gis.ru/spb/firm/1", True),
        ("https://www.openstreetmap.org/node/1", False),
        ("https://2gis.ru.evil.example/firm/1", False),
        ("https://evil.example/2gis.ru", False),
        ("javascript:alert(1)", False),
        ("tg://user?id=1", False),
        ("2gis.ru/spb/firm/1", False),
        ("https://2gis.ru/a b", False),
        ("https://2gis.ru/" + "a" * 600, False),
        ("https://[bad", False),
        ("", False),
        (None, False),
    ],
)
def test_only_a_safe_link_to_2gis_goes_on_a_button(url: str | None, usable: bool) -> None:
    assert (twogis_card_url(url) is not None) is usable


# ---------- what is asked from the API ----------


@pytest.mark.parametrize(
    ("rubrics", "expected"),
    [
        ([{"name": "Первая"}, {"name": "Главная", "kind": "primary"}], "Главная"),
        ([{"name": "Первая"}, {"name": "Вторая"}], "Первая"),
        ([{"name": "  "}, "не объект", {"kind": "primary"}], None),
        ("не список", None),
        (None, None),
    ],
)
def test_rubric_is_read_safely(rubrics: object, expected: str | None) -> None:
    data = {"result": {"items": [{"id": "1", "name": "Орг", "rubrics": rubrics}]}}
    assert parse_catalog_items(data, None, "beauty", True)[0].rubric == expected


async def test_organization_with_a_phone_but_no_site_really_has_no_site(session_maker, monkeypatch) -> None:
    """With paid access 'no site' is a known fact again, unlike for an organization without any contacts."""
    data = {
        "result": {
            "items": [
                {
                    "id": "1",
                    "type": "branch",
                    "name": "С телефоном",
                    "contact_groups": [{"contacts": [{"type": "phone", "value": "+7 921 000-00-02"}]}],
                }
            ]
        }
    }
    places = parse_catalog_items(data, None, "beauty", True)
    assert places[0].contacts_known is True
    async with session_maker() as session:
        await pipeline.save_collected_places(session, places)
    assert (await _saved(session_maker))[0].site_status == "no_site"


# ---------- limits of a demo key ----------


def test_scraper_is_off_by_default_everywhere() -> None:
    settings = Settings(_env_file=None)
    assert "2gis_scraper" not in settings.COLLECT_PROVIDERS
    assert "2gis_scraper" not in settings.COLLECTOR_PROVIDERS
    assert settings.COLLECT_PROVIDERS.split(",")[0] == "2gis_cdp"
    example = Settings(_env_file=Path(__file__).parent.parent / ".env.example")
    assert "2gis_scraper" not in example.COLLECT_PROVIDERS
    assert "2gis_scraper" not in example.COLLECTOR_PROVIDERS


# ---------- no alarms when contacts are simply unavailable ----------


# ---------- quota of the key ----------


async def _used(session_maker, name: str = "2gis") -> int:
    async with session_maker() as session:
        state = (await get_provider_states(session)).get(name)
    return state.requests_total if state else 0


# ---------- requests are not spent on what has been read ----------


def _paged(total: int = 50, requests: list[httpx.Request] | None = None):
    """An API with `total` organizations, ten on a page, recording what was asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        page = int(request.url.params["page"])
        first = (page - 1) * 10 + 1
        return httpx.Response(200, json=_items(max(0, min(10, total - first + 1)), first=first, total=total))

    return handler


def _pages(requests: list[httpx.Request]) -> list[str]:
    return [request.url.params["page"] for request in requests]


# ---------- the key runs out ----------


async def test_status_explains_a_rejected_key_without_mentioning_blocks() -> None:
    state = ProviderState(
        name="2gis",
        consecutive_blocks=0,
        disabled_at=datetime.now(UTC),
        last_block_reason="2ГИС не принял ключ",
        requests_total=0,
    )
    line = formatters._provider_line("2gis", state, datetime.now(UTC))
    assert "⛔ 2gis: отключён (2ГИС не принял ключ)" in line
    assert "/provider_on 2gis" in line


# ---------- the status command ----------


async def _add_leads(session_maker, with_contacts: int, without: int) -> None:
    base = {"source": "2gis", "name": "Лид", "category": "beauty", "site_status": SITE_UNKNOWN, "score": 45}
    data = [
        {**base, "external_id": f"2gis_{number}", "name": f"Лид {number}", "crm_status": STATUS_NEW}
        | ({"phone": "+78125550101"} if number < with_contacts else {})
        for number in range(with_contacts + without)
    ]
    async with session_maker() as session:
        await save_new_leads(session, data)


async def test_status_of_a_server_with_an_external_collector_shows_the_stock_too(session_maker) -> None:
    await _add_leads(session_maker, with_contacts=1, without=1)

    async def report(_: RunReport) -> None:
        return None

    runner = CollectorRunner(session_maker, ProviderSet(), _config(), report, external_mode=True, batch_size=30)
    text_ = format_collector_status(await runner.get_status())
    assert "внешний сборщик" in text_
    assert "(с контактами: <b>1</b>, без контактов: <b>1</b>)" in text_
    assert "меньше чем на день" in text_
    assert "API 2ГИС" not in text_


async def test_lead_counts_treat_blank_contacts_as_none(session_maker) -> None:
    await _add_leads(session_maker, with_contacts=0, without=2)
    async with session_maker() as session:
        lead = await session.get(Lead, 1)
        lead.phone = "   "
        await session.commit()
        assert await count_new_leads_with_contacts(session) == 0
        assert await count_new_leads(session) == 2


# ---------- handing out ----------


def test_card_of_a_lead_without_contacts() -> None:
    lead = _lead(rubric="Стоматологические клиники")
    card = format_issued_lead(lead)
    assert "<b>Организация 1</b>" in card
    assert "Стоматологические клиники" in card
    assert "Невский проспект, 10" in card
    assert "📞 <b>Телефон:</b> смотри в карточке" in card
    # Unknown is not 'no site', and the link is not repeated in the text.
    assert "Сайт" not in card
    assert "2gis.ru" not in card

    assert generate_pitch_text(lead).count("нет собственного сайта") == 0


def test_rubric_falls_back_to_the_niche() -> None:
    assert "Стоматологии и клиники" in format_issued_lead(_lead())


def test_buttons_of_a_lead_without_contacts() -> None:
    rows = _buttons(_lead())
    assert (rows[0][0].text, rows[0][0].url) == (OPEN_CARD_BUTTON_TEXT, "https://2gis.ru/spb/firm/1")
    statuses = [button.text for row in rows[1:3] for button in row]
    assert statuses == ["Написал", "Ответили", "Отказ", "Нет ответа"]
    assert [button.callback_data for button in rows[3]] == ["sv:1:yes", "sv:1:no"]
    assert rows[4][0].url.startswith("https://yandex.ru/search/")
    assert (rows[5][0].text, rows[5][0].callback_data) == (ADD_PHONE_BUTTON_TEXT, "lp:1")
    callbacks = [button.callback_data for row in rows[1:] for button in row if button.url is None]
    assert all(len(data.encode()) <= 64 for data in callbacks)


def test_known_phone_is_shown_as_before_and_needs_no_button_to_add_it() -> None:
    lead = _lead(phone="+78125550101", site_status="no_site")
    card = format_issued_lead(lead)
    assert "📞 <b>Телефон:</b> +78125550101" in card
    assert "смотри в карточке" not in card
    assert "Сайта нет (по данным источника)" in card
    assert ADD_PHONE_BUTTON_TEXT not in [button.text for row in _buttons(lead) for button in row]


@pytest.mark.parametrize("url", [None, "javascript:alert(1)", "https://2gis.ru/a b", "2gis.ru/firm/1"])
def test_lead_without_a_usable_link_has_no_button_and_no_false_promise(url: str | None) -> None:
    lead = _lead(source_url=url)
    assert OPEN_CARD_BUTTON_TEXT not in [button.text for row in _buttons(lead) for button in row]
    assert "смотри в карточке" not in format_issued_lead(lead)


def test_card_from_another_source_keeps_its_text_link() -> None:
    lead = _lead(source="osm", source_url="https://www.openstreetmap.org/node/1", site_status="no_site")
    assert "Карточка OpenStreetMap</a>" in format_issued_lead(lead)
    assert OPEN_CARD_BUTTON_TEXT not in [button.text for row in _buttons(lead) for button in row]


def test_card_with_huge_fields_fits_telegram_limit() -> None:
    lead = _lead(name="Я" * 5000, rubric="Р" * 5000, address="А" * 5000, socials="С" * 5000)
    assert len(format_issued_lead(lead)) <= formatters.MAX_MESSAGE_LENGTH


def test_browsing_card_does_not_call_an_unknown_site_working() -> None:
    card = format_lead_card(_lead())
    assert "Есть сайт" not in card
    assert "смотри в карточке" in card
    keyboard = get_lead_card_keyboard(1, card_url="https://2gis.ru/spb/firm/1")
    assert any(button.url == "https://2gis.ru/spb/firm/1" for row in keyboard.inline_keyboard for button in row)


async def test_leads_with_contacts_go_first_and_the_order_is_stable_inside(session_maker) -> None:
    base = {"source": "2gis", "category": "beauty", "site_status": SITE_UNKNOWN, "score": 45, "crm_status": STATUS_NEW}
    now = datetime.now(UTC)
    rows = [
        ("без-1", {}),
        ("телефон", {"phone": "+78125550101"}),
        ("без-2", {}),
        ("сайт", {"raw_website": "https://a.example"}),
        ("соцсеть", {"socials": "https://vk.com/a"}),
        ("пустой", {"phone": "  "}),
    ]
    data = [
        {**base, "external_id": f"2gis_{index}", "name": name, "created_at": now + timedelta(seconds=index), **extra}
        for index, (name, extra) in enumerate(rows)
    ]
    async with session_maker() as session:
        await save_new_leads(session, data)

    async with session_maker() as session:
        first = await issue_next_leads(session, 4)
        second = await issue_next_leads(session, 4)
    assert [lead.name for lead in first] == ["телефон", "сайт", "соцсеть", "без-1"]
    assert [lead.name for lead in second] == ["без-2", "пустой"]


async def test_parallel_batches_of_leads_without_contacts_do_not_overlap(session_maker) -> None:
    await _add_leads(session_maker, with_contacts=5, without=25)

    async def batch() -> list[int]:
        async with session_maker() as session:
            return [lead.id for lead in await issue_next_leads(session, 10)]

    results = await asyncio.gather(*(batch() for _ in range(5)))
    ids = [lead_id for result in results for lead_id in result]
    assert len(ids) == len(set(ids)) == 30


@pytest.fixture
def issue_env(session_maker, monkeypatch: pytest.MonkeyPatch):
    async def no_pause(_: float) -> None:
        return None

    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(issue.asyncio, "sleep", no_pause)
    issue._issuing_to.clear()
    issue._phone_prompts.clear()
    return session_maker


def _chat_message(chat_id: int = ADMIN) -> MagicMock:
    message = MagicMock(spec=Message)
    message.chat = Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")
    message.from_user = User(id=ADMIN, is_bot=False, first_name="Анна")
    message.message_id = 500
    message.answer = AsyncMock(return_value=MagicMock(message_id=777))
    message.bot = MagicMock()
    message.bot.edit_message_text = AsyncMock()
    message.bot.send_message = AsyncMock()
    return message


def _batch_runner() -> MagicMock:
    runner = MagicMock()
    runner.request_topup = AsyncMock(return_value=MagicMock(outcome=None, retry_at=None))
    runner.check_pool = AsyncMock()
    return runner


async def test_next_batch_sends_a_card_with_a_link_button(issue_env) -> None:
    async with issue_env() as session:
        await pipeline.save_collected_places(session, [_place(rubric="Стоматологические клиники")])

    message = _chat_message()
    await issue.handle_next_batch(message, _batch_runner())

    card_call = message.answer.await_args_list[0]
    assert "смотри в карточке" in card_call.args[0]
    button = card_call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.url) == (OPEN_CARD_BUTTON_TEXT, "https://2gis.ru/spb/firm/1")
    assert (await _saved(issue_env))[0].crm_status == STATUS_ISSUED


async def test_status_button_keeps_the_link_on_the_card(issue_env) -> None:
    async with issue_env() as session:
        await pipeline.save_collected_places(session, [_place()])
        lead_id = (await issue_next_leads(session, 1))[0].id

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ls:{lead_id}:contacted"
    callback.answer = AsyncMock()
    callback.from_user = User(id=ADMIN, is_bot=False, first_name="Анна")
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()

    await issue.handle_lead_status(callback)

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://2gis.ru/spb/firm/1"
    assert markup.inline_keyboard[1][0].text == "✅ Написал"
    assert markup.inline_keyboard[-2][0].text == ADD_PHONE_BUTTON_TEXT
    assert markup.inline_keyboard[-1][0].text == "↩️ Вернуть в пул"


async def test_refused_card_returns_to_the_pool_without_losing_the_others(issue_env) -> None:
    async with issue_env() as session:
        await pipeline.save_collected_places(session, [_place(1), _place(2)])

    message = _chat_message()
    refused = TelegramBadRequest(method=MagicMock(spec=SendMessage), message="BUTTON_URL_INVALID")
    message.answer = AsyncMock(side_effect=[refused, MagicMock(), MagicMock()])
    await issue.handle_next_batch(message, _batch_runner())

    statuses = {lead.id: lead.crm_status for lead in await _saved(issue_env)}
    assert statuses == {1: STATUS_NEW, 2: STATUS_ISSUED}


# ---------- adding a phone by hand ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (921) 000-00-02", "+79210000002"),
        ("8 812 555-01-01", "+78125550101"),
        ("9210000002", "+79210000002"),
        ("  +7 921 000 00 02 ", "+79210000002"),
        ("+375 29 123-45-67", "+375291234567"),
        ("", None),
        (None, None),
        ("нет номера", None),
        ("+7921", None),
        ("12345", None),
        ("8921000000212", None),
        ("+792100000021", None),
        ("89210000002 доб. 5", None),
        ("<b>89210000002</b>", None),
    ],
)
def test_manual_phone_format(raw: str | None, expected: str | None) -> None:
    assert parse_manual_phone(raw) == expected


async def _issued_lead(session_maker) -> int:
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [_place()])
        return (await issue_next_leads(session, 1))[0].id


def _add_phone_callback(lead_id: int, chat_id: int = ADMIN) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"lp:{lead_id}"
    callback.answer = AsyncMock()
    callback.from_user = User(id=ADMIN, is_bot=False, first_name="Анна <Админ>")
    callback.message = _chat_message(chat_id)
    return callback


def _reply_message(text_: str, prompt_id: int = 777, chat_id: int = ADMIN) -> MagicMock:
    message = _chat_message(chat_id)
    message.text = text_
    message.reply_to_message = MagicMock(message_id=prompt_id)
    return message


async def test_adding_a_phone_saves_it_and_updates_the_card(issue_env) -> None:
    lead_id = await _issued_lead(issue_env)
    callback = _add_phone_callback(lead_id)
    await issue.handle_add_phone(callback)

    asked = callback.message.answer.await_args
    assert asked.args[0].startswith(issue.PHONE_PROMPT_PREFIX)
    assert "Анна &lt;Админ&gt;" in asked.args[0]
    assert isinstance(asked.kwargs["reply_markup"], ForceReply)
    assert asked.kwargs["reply_markup"].selective is True
    callback.answer.assert_awaited_once()

    reply = _reply_message("8 (921) 000-00-02")
    await issue.handle_phone_reply(reply)

    assert (await _saved(issue_env))[0].phone == "+79210000002"
    assert "+79210000002" in reply.answer.await_args.args[0]
    edited = reply.bot.edit_message_text.await_args
    assert edited.kwargs["message_id"] == 500  # the card, not the question
    assert "📞 <b>Телефон:</b> +79210000002" in edited.args[0]
    buttons = [button.text for row in edited.kwargs["reply_markup"].inline_keyboard for button in row]
    assert OPEN_CARD_BUTTON_TEXT in buttons
    assert ADD_PHONE_BUTTON_TEXT not in buttons
    assert issue._phone_prompts == {}


async def test_wrong_number_is_refused_and_can_be_corrected(issue_env) -> None:
    lead_id = await _issued_lead(issue_env)
    await issue.handle_add_phone(_add_phone_callback(lead_id))

    wrong = _reply_message("позвоню завтра")
    await issue.handle_phone_reply(wrong)
    assert "Не похоже" in wrong.answer.await_args.args[0]
    assert (await _saved(issue_env))[0].phone is None
    wrong.bot.edit_message_text.assert_not_awaited()

    fixed = _reply_message("+7 921 000-00-02")
    await issue.handle_phone_reply(fixed)
    assert (await _saved(issue_env))[0].phone == "+79210000002"


async def test_question_forgotten_after_a_restart_is_explained(issue_env) -> None:
    await _issued_lead(issue_env)
    reply = _reply_message("+7 921 000-00-02", prompt_id=12345)
    await issue.handle_phone_reply(reply)
    assert "устарел" in reply.answer.await_args.args[0]
    assert (await _saved(issue_env))[0].phone is None


async def test_old_question_expires(issue_env, monkeypatch) -> None:
    lead_id = await _issued_lead(issue_env)
    await issue.handle_add_phone(_add_phone_callback(lead_id))
    real = issue.time.monotonic()
    monkeypatch.setattr(issue.time, "monotonic", lambda: real + issue._PHONE_PROMPT_TTL_SECONDS + 1)
    reply = _reply_message("+7 921 000-00-02")
    await issue.handle_phone_reply(reply)
    assert "устарел" in reply.answer.await_args.args[0]
    assert (await _saved(issue_env))[0].phone is None


async def test_the_list_of_questions_is_bounded(issue_env, monkeypatch) -> None:
    monkeypatch.setattr(issue, "_MAX_PHONE_PROMPTS", 3)
    for prompt_id in range(10):
        issue._remember_phone_prompt(ADMIN, prompt_id, 1, 2)
    assert len(issue._phone_prompts) == 3
    assert (ADMIN, 9) in issue._phone_prompts


@pytest.mark.parametrize("data", ["lp:", "lp:abc", "lp:1:2", "lp:999"])
async def test_stale_add_phone_button(issue_env, data: str) -> None:
    callback = _add_phone_callback(1)
    callback.data = data
    await issue.handle_add_phone(callback)
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    callback.message.answer.assert_not_awaited()


async def test_two_quick_replies_do_not_fail(issue_env) -> None:
    lead_id = await _issued_lead(issue_env)
    await issue.handle_add_phone(_add_phone_callback(lead_id))
    await asyncio.gather(
        issue.handle_phone_reply(_reply_message("+7 921 000-00-02")),
        issue.handle_phone_reply(_reply_message("+7 921 000-00-02")),
    )
    assert (await _saved(issue_env))[0].phone == "+79210000002"


async def test_card_update_failure_keeps_the_saved_phone(issue_env) -> None:
    lead_id = await _issued_lead(issue_env)
    await issue.handle_add_phone(_add_phone_callback(lead_id))
    reply = _reply_message("+7 921 000-00-02")
    reply.bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(spec=SendMessage), message="message to edit not found")
    )
    await issue.handle_phone_reply(reply)
    assert (await _saved(issue_env))[0].phone == "+79210000002"


@pytest.fixture
def guarded_dispatcher(issue_env, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dispatcher]:
    """The handlers behind the same admin check as in main.py."""
    monkeypatch.setattr("src.bot.middlewares.settings", Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=ADMIN))
    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    issue.router._parent_router = None  # another test may have left it attached
    dp.include_router(issue.router)
    yield dp
    issue.router._parent_router = None
    dp.sub_routers.clear()


def _reply_update(user_id: int, text_: str, prompt_id: int, prompt_text: str) -> Update:
    chat = Chat(id=GROUP, type="supergroup")
    when = datetime(2026, 11, 5, tzinfo=UTC)
    prompt = Message(
        message_id=prompt_id,
        date=when,
        chat=chat,
        text=prompt_text,
        from_user=User(id=1, is_bot=True, first_name="Бот"),
    )
    message = Message(
        message_id=prompt_id + 1,
        date=when,
        chat=chat,
        text=text_,
        reply_to_message=prompt,
        from_user=User(id=user_id, is_bot=False, first_name="Человек"),
    )
    return Update(update_id=prompt_id, message=message)


def _press_update(user_id: int, data: str) -> Update:
    chat = Chat(id=GROUP, type="supergroup")
    card = Message(message_id=500, date=datetime(2026, 11, 5, tzinfo=UTC), chat=chat, text="карточка")
    callback = CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="Человек"),
        chat_instance="c",
        data=data,
        message=card,
    )
    return Update(update_id=1, callback_query=callback)


def _patch_telegram(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    calls = MagicMock()
    calls.answer = AsyncMock(return_value=MagicMock(message_id=777))
    calls.reply = AsyncMock()
    calls.callback_answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", calls.answer)
    monkeypatch.setattr(Message, "reply", calls.reply)
    monkeypatch.setattr(CallbackQuery, "answer", calls.callback_answer)
    return calls


async def test_stranger_cannot_press_add_phone(guarded_dispatcher, issue_env, monkeypatch) -> None:
    calls = _patch_telegram(monkeypatch)
    lead_id = await _issued_lead(issue_env)
    await guarded_dispatcher.feed_update(MagicMock(), _press_update(STRANGER, f"lp:{lead_id}"))
    assert calls.callback_answer.await_args.args[0] == "⛔ Нет доступа"
    calls.answer.assert_not_awaited()
    assert issue._phone_prompts == {}


async def test_stranger_cannot_set_a_phone_by_replying(guarded_dispatcher, issue_env, monkeypatch) -> None:
    calls = _patch_telegram(monkeypatch)
    lead_id = await _issued_lead(issue_env)
    issue._remember_phone_prompt(GROUP, 777, lead_id, 500)
    update = _reply_update(STRANGER, "+7 921 000-00-02", 777, f"{issue.PHONE_PROMPT_PREFIX} «Организация 1»")
    await guarded_dispatcher.feed_update(MagicMock(), update)
    assert (await _saved(issue_env))[0].phone is None
    assert (GROUP, 777) in issue._phone_prompts
    assert calls.reply.await_args.args[0] == "⛔ Нет доступа"


async def test_admin_goes_all_the_way_through_the_dispatcher(guarded_dispatcher, issue_env, monkeypatch) -> None:
    calls = _patch_telegram(monkeypatch)
    lead_id = await _issued_lead(issue_env)

    await guarded_dispatcher.feed_update(MagicMock(), _press_update(ADMIN, f"lp:{lead_id}"))
    question = calls.answer.await_args.args[0]
    assert question.startswith(issue.PHONE_PROMPT_PREFIX)

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    update = _reply_update(ADMIN, "+7 921 000-00-02", 777, question)
    await guarded_dispatcher.feed_update(bot, update)
    assert (await _saved(issue_env))[0].phone == "+79210000002"


async def test_ordinary_replies_are_not_taken_for_a_phone(guarded_dispatcher, issue_env, monkeypatch) -> None:
    calls = _patch_telegram(monkeypatch)
    update = _reply_update(ADMIN, "+7 921 000-00-02", 777, "какое-то другое сообщение бота")
    await guarded_dispatcher.feed_update(MagicMock(), update)
    calls.answer.assert_not_awaited()
    calls.reply.assert_not_awaited()


# ---------- files from a collector on another computer ----------


def _record(**overrides) -> dict:
    return {
        "source": "2gis",
        "source_id": "70001",
        "name": "Организация",
        "address": "Невский проспект, 10",
        "phones": [],
        "website": None,
        "rubric": "dentist_clinic",
        "rubric_name": "Стоматологические клиники",
        "source_url": "https://2gis.ru/spb/firm/70001",
    } | overrides


def _file(*records: dict) -> bytes:
    document = build_document(list(records), datetime(2026, 11, 5, tzinfo=UTC), "home")
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_file_lead_without_contacts_follows_the_setting() -> None:
    allowed = parse_lead_file(_file(_record()), 1024 * 1024, 10, require_contact=False)
    assert len(allowed.places) == 1
    assert allowed.places[0].contacts_known is False
    assert allowed.places[0].rubric == "Стоматологические клиники"

    refused = parse_lead_file(_file(_record()), 1024 * 1024, 10, require_contact=True)
    assert refused.places == []
    assert refused.rejected == {REASON_NO_CONTACTS: 1}


def test_file_lead_from_another_source_still_needs_contacts() -> None:
    parsed = parse_lead_file(_file(_record(source="osm")), 1024 * 1024, 10, require_contact=False)
    assert parsed.rejected == {REASON_NO_CONTACTS: 1}


def test_file_from_an_older_collector_without_a_rubric_name_is_fine() -> None:
    record = _record(phones=["+78125550101"])
    del record["rubric_name"]
    assert parse_lead_file(_file(record), 1024 * 1024, 10).places[0].rubric is None


async def test_from_the_collector_through_a_file_to_the_card(session_maker, tmp_path: Path, monkeypatch) -> None:
    """The home computer case: without contacts the lead must survive export and import."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'home.db').as_posix()}")
    await init_db(engine)
    home = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with home() as session:
        await pipeline.save_collected_places(session, [_place(rubric="Клиника")])
    exported = await export_new_leads(home, tmp_path, "spb", 100)
    assert json.loads(exported.files[0].read_text(encoding="utf-8"))["leads"][0]["rubric_name"] == "Клиника"
    await engine.dispose()

    service = ImportService(session_maker, AsyncMock(), tmp_path / "inbox", 1024 * 1024, 100)
    result = await service.import_bytes(exported.files[0].read_bytes(), "leads.json", SOURCE_TELEGRAM)
    assert (result.ok, result.saved_new, result.rejected_total) == (True, 1, 0)

    lead = (await _saved(session_maker))[0]
    assert lead.site_status == SITE_UNKNOWN
    assert lead.rubric == "Клиника"
    assert lead.source_url == "https://2gis.ru/spb/firm/1"
    assert lead.imported_at is not None
    assert "смотри в карточке" in format_issued_lead(lead)
    assert lead_to_record(lead, "spb")["phones"] == []


async def test_server_that_requires_contacts_refuses_such_a_file(session_maker, tmp_path: Path) -> None:
    service = ImportService(session_maker, AsyncMock(), tmp_path / "inbox", 1024 * 1024, 100, require_contact=True)
    result = await service.import_bytes(_file(_record()), "leads.json", SOURCE_TELEGRAM)
    assert result.saved_new == 0
    assert result.rejected == {REASON_NO_CONTACTS: 1}


# ---------- captcha: only a stop, no way around ----------


def test_the_browser_module_has_nothing_that_hides_automation() -> None:
    source = (Path(__file__).parent.parent / "src" / "services" / "scraper" / "browser.py").read_text(encoding="utf-8")
    for marker in ("add_init_script", "AutomationControlled", "navigator", "window.chrome", "recaptcha", "mouse"):
        assert marker not in source


# ---------- remembering how far a source was read ----------


class PagedProvider(BaseMapProvider):
    """A source with `total` organizations, ten on a page, that records the pages it was asked for."""

    name = "paged"
    progress_scope = "full"

    def __init__(self, total: int = 50, fail_page: int | None = None, auth_error: bool = False) -> None:
        self.total = total
        self.fail_page = fail_page
        self.auth_error = auth_error
        self.asked: list[tuple[str, int]] = []
        if auth_error:
            self.auth_failure_hint = "источник не принял доступ"

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.asked.append((query.category, page))
        if self.auth_error:
            raise ProviderAuthError("SECRET отклонён")
        if page == self.fail_page:
            raise ProviderTemporaryError("сеть")
        first = (page - 1) * 10 + 1
        count = max(0, min(10, self.total - first + 1))
        places = [
            _place(number, source="paged", external_id=f"paged_{number}") for number in range(first, first + count)
        ]
        return SearchPage(places=places, has_more=first + count <= self.total and count > 0)


def _asked_pages(provider: PagedProvider) -> list[int]:
    return [page for _, page in provider.asked]


async def test_next_run_continues_from_the_page_where_the_last_one_stopped(session_maker) -> None:
    provider = PagedProvider()
    config = _config(COLLECT_MAX_PAGES=2)

    first = await collect_leads(session_maker, [provider], config)
    second = await collect_leads(session_maker, [provider], config)
    third = await collect_leads(session_maker, [provider], config)

    assert _asked_pages(provider) == [1, 2, 3, 4, 5]
    assert [run.saved_new for run in (first, second, third)] == [20, 20, 10]


async def test_walked_target_costs_nothing_and_the_report_says_why(session_maker) -> None:
    provider = PagedProvider(total=15)
    config = _config(COLLECT_MAX_PAGES=5)

    await collect_leads(session_maker, [provider], config)
    assert _asked_pages(provider) == [1, 2]

    again = await collect_leads(session_maker, [provider], config)
    assert _asked_pages(provider) == [1, 2]
    assert again.requests == 0
    assert again.errors == 0
    assert "пройдены до конца" in again.skipped["paged"]


async def test_walked_target_is_read_again_after_the_recheck_period(session_maker) -> None:
    provider = PagedProvider(total=5)
    config = _config(COLLECT_RECHECK_DAYS=30)
    await collect_leads(session_maker, [provider], config)

    async with session_maker() as session:
        row = (await session.scalars(select(SearchProgress))).one()
        row.exhausted_at = datetime.now(UTC) - timedelta(days=31)
        await session.commit()

    await collect_leads(session_maker, [provider], config)
    assert _asked_pages(provider) == [1, 1]


async def test_failed_page_is_requested_again(session_maker) -> None:
    provider = PagedProvider(fail_page=2)
    config = _config(COLLECT_MAX_PAGES=2)
    await collect_leads(session_maker, [provider], config)
    provider.fail_page = None
    await collect_leads(session_maker, [provider], config)
    assert _asked_pages(provider)[:3] == [1, 2, 2]  # the failed page 2 is asked again, not skipped


async def test_position_is_not_saved_if_the_leads_were_not(session_maker, monkeypatch) -> None:
    """Otherwise the pages of a failed run would be skipped for good."""
    provider = PagedProvider()
    config = _config(COLLECT_MAX_PAGES=1)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("database")

    with monkeypatch.context() as patch:
        patch.setattr("src.services.collector.save_collected_places", fail)
        with pytest.raises(RuntimeError):
            await collect_leads(session_maker, [provider], config)

    await collect_leads(session_maker, [provider], config)
    assert _asked_pages(provider) == [1, 1]


async def test_targets_keep_their_own_positions(session_maker) -> None:
    provider = PagedProvider()
    config = _config(COLLECT_QUERIES="dentist_clinic,beauty", COLLECT_MAX_PAGES=1)
    await collect_leads(session_maker, [provider], config)
    await collect_leads(session_maker, [provider], config)
    assert len(set(provider.asked)) == 4


async def test_page_is_not_cut_short_when_it_is_remembered_as_read(session_maker) -> None:
    result = await collect_leads(session_maker, [PagedProvider()], _config(COLLECT_MAX_RESULTS_PER_QUERY=3))
    assert result.saved_new == 10


async def test_sources_without_a_scope_repeat_as_before(session_maker) -> None:
    provider = PagedProvider(total=5)
    provider.progress_scope = None
    await collect_leads(session_maker, [provider], _config())
    await collect_leads(session_maker, [provider], _config())
    assert _asked_pages(provider) == [1, 1]


# ---------- a source that does not accept the access ----------


async def test_rejected_access_switches_the_source_off_and_stops_asking(session_maker) -> None:
    provider = PagedProvider(auth_error=True)
    config = _config(COLLECT_QUERIES="dentist_clinic,beauty")
    first = await collect_leads(session_maker, [provider], config)

    assert len(provider.asked) == 1
    assert len(first.alerts) == 1
    assert "/provider_on paged" in first.alerts[0]
    assert "SECRET" not in first.alerts[0]
    async with session_maker() as session:
        state = (await get_provider_states(session))["paged"]
    assert state.disabled_at is not None
    assert state.consecutive_blocks < MAX_CONSECUTIVE_BLOCKS

    second = await collect_leads(session_maker, [provider], config)
    assert len(provider.asked) == 1  # nothing more goes out
    assert "paged" in second.skipped

    async with session_maker() as session:
        await enable_provider(session, "paged")
    await collect_leads(session_maker, [provider], config)
    assert len(provider.asked) == 2  # the admin's command lets it try again


async def test_status_explains_a_switched_off_source_without_mentioning_blocks() -> None:
    state = ProviderState(
        name="paged",
        consecutive_blocks=0,
        disabled_at=datetime.now(UTC),
        last_block_reason="источник не принял доступ",
    )
    line = formatters._provider_line("paged", state, datetime.now(UTC))
    assert "⛔ paged: отключён (источник не принял доступ)" in line
    assert "/provider_on paged" in line
