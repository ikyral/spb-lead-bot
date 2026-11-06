"""Tests for telling leads that need a website from those that do not: statuses, verdicts, order, chains, niches."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from src.bot.formatters import format_issued_lead, format_lead_card, generate_pitch_text
from src.bot.handlers import issue, leads
from src.bot.keyboards import (
    SEARCH_BUTTON_TEXT,
    SITE_NO_BUTTON_TEXT,
    SITE_YES_BUTTON_TEXT,
    get_issued_lead_keyboard,
)
from src.database.models import CONTACT_REPLIED, STATUS_ISSUED, STATUS_NEW, Lead
from src.database.repo import count_new_leads, get_leads, issue_next_leads, set_site_verdict
from src.services.pipeline import save_collected_places
from src.services.providers.base import RawPlace
from src.services.site_status import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    ISSUE_GROUPS,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNCERTAIN,
    SITE_UNKNOWN,
    SOURCE_MANUAL,
    confidence_of_check,
    issue_group,
    source_of_status,
    yandex_search_url,
)


def _lead(**overrides) -> Lead:
    values = {
        "id": 1,
        "external_id": "2gis_1",
        "source": "2gis",
        "name": "Улыбка",
        "category": "dentist_clinic",
        "site_status": SITE_UNKNOWN,
        "score": 50,
        "crm_status": "new",
    }
    return Lead(**{**values, **overrides})


# ---------- what a card says ----------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"site_status": SITE_NO_SITE}, "🌐 Сайта нет (по данным источника)"),
        ({"site_status": SITE_NO_SITE, "site_source": SOURCE_MANUAL}, "✅ Сайта нет (проверено вручную)"),
        ({"site_status": SITE_NOT_FOUND}, "🌐 Сайт не найден в поиске"),
        ({"site_status": SITE_UNCERTAIN, "site_url": "example.ru"}, "🌐 Возможно, сайт: example.ru, проверьте"),
        ({"site_status": SITE_UNKNOWN}, "🌐 Статус неизвестен"),
        ({"site_status": SITE_FOUND, "site_url": "example.ru"}, "🌐 Сайт есть (example.ru)"),
    ],
)
@pytest.mark.parametrize("card", [format_issued_lead, format_lead_card])
def test_card_says_how_much_is_known(card, overrides: dict, expected: str) -> None:
    assert expected in card(_lead(**overrides))


@pytest.mark.parametrize("status", [SITE_UNKNOWN, SITE_NOT_FOUND, SITE_UNCERTAIN])
@pytest.mark.parametrize("show", [format_issued_lead, format_lead_card, generate_pitch_text])
def test_nothing_claims_there_is_no_site_unless_it_is_confirmed(show, status: str) -> None:
    text = show(_lead(site_status=status)).lower()
    assert "сайта нет" not in text
    assert "нет сайта" not in text
    assert "нет собственного" not in text


def test_candidate_address_cannot_break_the_markup() -> None:
    card = format_issued_lead(_lead(site_status=SITE_UNCERTAIN, site_url="<b>x</b>.ru"))
    assert "&lt;b&gt;x&lt;/b&gt;.ru" in card


def test_a_person_can_tell_the_owner_more_than_a_source_can() -> None:
    assert "нет собственного сайта" in generate_pitch_text(_lead(site_status=SITE_NO_SITE, site_source=SOURCE_MANUAL))
    assert "нет собственного сайта" not in generate_pitch_text(_lead(site_status=SITE_NO_SITE, site_source="osm"))


# ---------- what the statuses mean ----------


def test_order_of_groups() -> None:
    assert ISSUE_GROUPS[0] == (SITE_NO_SITE, SITE_DEAD, SITE_SOCIAL_ONLY)
    positions = [issue_group(status) for status in (SITE_DEAD, SITE_NOT_FOUND, SITE_UNCERTAIN, SITE_UNKNOWN)]
    assert positions == sorted(positions)
    assert len(set(positions)) == 4


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        (SITE_DEAD, "osm", CONFIDENCE_HIGH),
        (SITE_SOCIAL_ONLY, "2gis", CONFIDENCE_HIGH),
        (SITE_NO_SITE, "2gis", CONFIDENCE_MEDIUM),
        (SITE_NO_SITE, "osm", CONFIDENCE_LOW),  # OSM often just lacks the tag
        (SITE_UNKNOWN, "2gis", None),
    ],
)
def test_confidence_of_a_check(status: str, source: str, expected: str | None) -> None:
    assert confidence_of_check(status, source) == expected


def test_only_known_sources_are_named() -> None:
    assert source_of_status("2gis") == "2gis"
    assert source_of_status("osm") == "osm"
    assert source_of_status("csv") is None


def test_search_link_is_an_ordinary_encoded_link() -> None:
    url = yandex_search_url("Кафе & Бар", "Невский, 1")
    assert url.startswith("https://yandex.ru/search/?text=")
    assert " " not in url and "&" not in url.removeprefix("https://yandex.ru/search/?")
    assert "%D0%A1%D0%B0%D0%BD%D0%BA%D1%82" in yandex_search_url("Кафе", None)  # the city, when no address is known
    assert len(yandex_search_url("я" * 1000, "б" * 1000)) < 1500


# ---------- a verdict in one click ----------


async def _save(session_maker, **overrides) -> int:
    values = {"external_id": f"2gis_{datetime.now(UTC).timestamp()}", **overrides}
    async with session_maker() as session:
        lead = _lead(**{**values, "id": None})
        session.add(lead)
        await session.commit()
        return lead.id


def _callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


def _button_texts(lead: Lead) -> list[str]:
    return [button.text for row in get_issued_lead_keyboard(lead).inline_keyboard for button in row]


def test_card_has_the_verdict_buttons_and_an_ordinary_search_link() -> None:
    lead = _lead()
    texts = _button_texts(lead)
    assert {SITE_YES_BUTTON_TEXT, SITE_NO_BUTTON_TEXT, SEARCH_BUTTON_TEXT} <= set(texts)
    buttons = [button for row in get_issued_lead_keyboard(lead).inline_keyboard for button in row]
    search = next(button for button in buttons if button.text == SEARCH_BUTTON_TEXT)
    assert search.url.startswith("https://yandex.ru/search/?text=")
    assert search.callback_data is None


def test_the_verdict_given_is_marked() -> None:
    assert f"☑️ {SITE_NO_BUTTON_TEXT}" in _button_texts(_lead(site_status=SITE_NO_SITE, site_source=SOURCE_MANUAL))
    assert f"☑️ {SITE_YES_BUTTON_TEXT}" in _button_texts(_lead(site_status=SITE_FOUND, site_source=SOURCE_MANUAL))
    # What a source says is not a verdict of a person.
    assert f"☑️ {SITE_NO_BUTTON_TEXT}" not in _button_texts(_lead(site_status=SITE_NO_SITE, site_source="osm"))


async def test_has_site_verdict_takes_a_new_lead_out_of_the_pool_for_good(session_maker) -> None:
    lead_id = await _save(session_maker, raw_website=None)
    async with session_maker() as session:
        assert await count_new_leads(session) == 1
        lead, changed = await set_site_verdict(session, lead_id, has_site=True)
        assert changed is True
        assert (lead.site_status, lead.site_source, lead.site_confidence) == (SITE_FOUND, SOURCE_MANUAL, "high")
        assert lead.site_checked_at is not None
        assert await count_new_leads(session) == 0
        assert await issue_next_leads(session, 10) == []
        assert await get_leads(session, crm_status=STATUS_NEW, min_score=0) == []


async def test_a_lead_with_a_site_does_not_come_back_with_the_next_collection(session_maker, monkeypatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr("src.services.pipeline.analyze_website", fake_analyze)
    place = RawPlace(external_id="2gis_7", source="2gis", name="Клиника", category="beauty", contacts_known=False)
    async with session_maker() as session:
        await save_collected_places(session, [place])
        lead = (await get_leads(session, crm_status=STATUS_NEW, min_score=0))[0]
        await set_site_verdict(session, lead.id, has_site=True)
        stats = await save_collected_places(session, [place])
        assert stats["saved_new"] == 0
        assert await issue_next_leads(session, 10) == []


async def test_no_site_verdict_is_high_confidence_and_stays_in_the_pool(session_maker) -> None:
    lead_id = await _save(session_maker)
    async with session_maker() as session:
        lead, _ = await set_site_verdict(session, lead_id, has_site=False)
        assert (lead.site_status, lead.site_source, lead.site_confidence) == (SITE_NO_SITE, SOURCE_MANUAL, "high")
        assert [issued.id for issued in await issue_next_leads(session, 10)] == [lead_id]


async def test_repeating_a_verdict_changes_nothing(session_maker) -> None:
    lead_id = await _save(session_maker)
    async with session_maker() as session:
        first, changed = await set_site_verdict(session, lead_id, has_site=True)
        assert changed is True
        checked_at = first.site_checked_at
        again, changed = await set_site_verdict(session, lead_id, has_site=True)
        assert changed is False
        assert again.site_checked_at == checked_at


async def test_verdict_can_be_corrected_and_a_missing_lead_is_reported(session_maker) -> None:
    lead_id = await _save(session_maker)
    async with session_maker() as session:
        await set_site_verdict(session, lead_id, has_site=True)
        lead, changed = await set_site_verdict(session, lead_id, has_site=False)
        assert changed is True
        assert lead.site_status == SITE_NO_SITE
        assert lead.site_url is None
        assert await set_site_verdict(session, 999, has_site=True) == (None, False)


async def test_verdict_does_not_touch_the_result_of_contacting(session_maker) -> None:
    lead_id = await _save(
        session_maker, crm_status=STATUS_ISSUED, issued_at=datetime.now(UTC), contact_status=CONTACT_REPLIED
    )
    async with session_maker() as session:
        lead, _ = await set_site_verdict(session, lead_id, has_site=True)
        assert lead.contact_status == CONTACT_REPLIED
        assert lead.crm_status == STATUS_ISSUED


@pytest.fixture
def _handlers_use_the_test_database(session_maker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(leads, "async_session_maker", session_maker)


@pytest.mark.usefixtures("_handlers_use_the_test_database")
async def test_button_on_a_handed_out_card_saves_and_updates_the_card(session_maker) -> None:
    lead_id = await _save(session_maker, crm_status=STATUS_ISSUED, issued_at=datetime.now(UTC))
    callback = _callback(f"sv:{lead_id}:yes")

    await issue.handle_site_verdict(callback)

    callback.answer.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert "Сайт есть" in text

    second = _callback(f"sv:{lead_id}:yes")
    await issue.handle_site_verdict(second)
    second.answer.assert_awaited_once_with("Эта отметка уже стоит")
    second.message.edit_text.assert_not_called()


@pytest.mark.usefixtures("_handlers_use_the_test_database")
@pytest.mark.parametrize("data", ["sv:abc:yes", "sv:1:maybe", "sv:1", "sv:999:yes"])
async def test_forged_or_stale_verdict_buttons_are_refused(data: str) -> None:
    callback = _callback(data)
    await issue.handle_site_verdict(callback)
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_called()


@pytest.mark.usefixtures("_handlers_use_the_test_database")
async def test_button_while_browsing_moves_to_the_next_lead(session_maker) -> None:
    first = await _save(session_maker, external_id="2gis_a", name="Первый", score=90)
    await _save(session_maker, external_id="2gis_b", name="Второй", score=80)
    callback = _callback(f"lead:{first}:site_yes:0")

    await leads.handle_lead_action(callback)

    assert "Второй" in callback.message.edit_text.await_args.args[0]
    async with session_maker() as session:
        assert await count_new_leads(session) == 1
