"""Tests for bot formatters, pitch generation, keyboards, and middleware."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User

from src.bot.formatters import format_lead_card, generate_pitch_text
from src.bot.keyboards import (
    CATEGORY_KEYS,
    DISTRICT_NAMES,
    get_categories_keyboard,
    get_districts_keyboard,
    get_export_keyboard,
    get_lead_card_keyboard,
    get_main_menu_keyboard,
)
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import settings
from src.database.models import Lead


def _make_dummy_lead(**kwargs) -> Lead:
    """Helper to create dummy Lead model instance."""
    defaults = {
        "id": 1,
        "external_id": "test_1",
        "source": "osm",
        "name": "Стоматология Улыбка",
        "category": "dentist_clinic",
        "district": "Центральный",
        "address": "Невский пр., 10",
        "phone": "+78121234567",
        "socials": "https://vk.com/smilespb",
        "raw_website": "http://smiledent.ru",
        "site_status": "dead_site",
        "score": 85,
        "crm_status": "new",
        "map_url": "https://yandex.ru/maps/org/123",
    }
    defaults.update(kwargs)
    return Lead(**defaults)


def test_format_lead_card_statuses():
    """Test format_lead_card output for various site statuses."""
    dead_lead = _make_dummy_lead(site_status="dead_site", raw_website="http://broken.spb.ru")
    card_dead = format_lead_card(dead_lead)
    assert "⚠️ Мертвый сайт / ошибка домена (http://broken.spb.ru)" in card_dead
    assert "🔥 85/100" in card_dead
    assert "Центральный" in card_dead
    assert "Невский пр., 10" in card_dead
    assert "+78121234567" in card_dead

    social_lead = _make_dummy_lead(site_status="social_only")
    card_social = format_lead_card(social_lead)
    assert "📱 Только соцсеть вместо сайта" in card_social

    no_site_lead = _make_dummy_lead(site_status="no_site")
    card_no_site = format_lead_card(no_site_lead)
    assert "❌ Нет сайта" in card_no_site


def test_format_lead_card_html_escaping():
    """Test that all user-supplied data in lead card is safely escaped."""
    malicious_lead = _make_dummy_lead(
        name='Кафе "Рога & Копыта" <script>alert("xss")</script>',
        district="Приморский & Выборгский <район>",
        address="ул. Мира <12> & Ленина",
        phone="<+78129999999>",
        socials="https://vk.com/group?a=1&b=2",
        raw_website="http://badsite.ru/?p=<script>",
        site_status="dead_site",
    )
    card = format_lead_card(malicious_lead)

    # Check that forbidden unescaped HTML tags do not appear
    assert "<script>" not in card
    assert "</script>" not in card
    assert "&lt;script&gt;" in card
    assert "&amp;" in card
    assert "&lt;район&gt;" in card
    assert "&lt;+78129999999&gt;" in card


def test_generate_pitch_text():
    """Test outreach pitch generation for dead_site, social_only, and no_site."""
    dead_lead = _make_dummy_lead(
        name="Клиника Дент",
        site_status="dead_site",
        raw_website="http://broken-dent.ru",
    )
    pitch_dead = generate_pitch_text(dead_lead)
    assert "Клиника Дент" in pitch_dead
    assert "http://broken-dent.ru" in pitch_dead
    assert "выдает ошибку" in pitch_dead or "не открывается" in pitch_dead

    social_lead = _make_dummy_lead(
        name="Салон Гламур",
        site_status="social_only",
        district="Петроградский",
    )
    pitch_social = generate_pitch_text(social_lead)
    assert "Салон Гламур" in pitch_social
    assert "Петроградский" in pitch_social
    assert "демо-прототип" in pitch_social
    assert "соцсеть" in pitch_social

    no_site_lead = _make_dummy_lead(
        name="Автосервис Мастер",
        site_status="no_site",
    )
    pitch_no_site = generate_pitch_text(no_site_lead)
    assert "Автосервис Мастер" in pitch_no_site
    assert "сайт под ключ" in pitch_no_site


def test_keyboards_callback_data_limits():
    """Verify that all generated callback_data payloads are <= 64 bytes."""
    # 1. Main menu reply keyboard
    reply_kb = get_main_menu_keyboard()
    button_texts = [btn.text for row in reply_kb.keyboard for btn in row]
    assert "🔍 Поиск по СПб" in button_texts
    assert "📋 Новые лиды" in button_texts
    assert "💼 В работе" in button_texts
    assert "📊 Статистика и CSV" in button_texts

    # 2. Districts keyboard
    dist_kb = get_districts_keyboard()
    for row in dist_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data is not None
            assert len(btn.callback_data.encode("utf-8")) <= 64
            assert btn.callback_data.startswith("dist:")

    # 3. Categories keyboard
    for d_idx in range(len(DISTRICT_NAMES)):
        cat_kb = get_categories_keyboard(d_idx)
        for row in cat_kb.inline_keyboard:
            for btn in row:
                assert btn.callback_data is not None
                assert len(btn.callback_data.encode("utf-8")) <= 64

    # 4. Lead card keyboard with pagination & map link
    for offset in (0, 1, 9999):
        for has_prev, has_next in ((False, False), (True, False), (False, True), (True, True)):
            lead_kb = get_lead_card_keyboard(
                lead_id=987654,
                offset=offset,
                map_url="https://yandex.ru/maps/org/12345678",
                has_prev=has_prev,
                has_next=has_next,
            )
            for row in lead_kb.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        raw_bytes = btn.callback_data.encode("utf-8")
                        assert len(raw_bytes) <= 64
                        parts = btn.callback_data.split(":")
                        assert parts[0] == "lead"
                        assert parts[1] == "987654"
                        assert parts[2] in ("work", "reject", "pitch", "prev", "next")
                    elif btn.url:
                        assert btn.url.startswith("https://")

    # 5. Export keyboard
    export_kb = get_export_keyboard()
    for row in export_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data is not None
            assert len(btn.callback_data.encode("utf-8")) <= 64
            assert btn.callback_data in ("export:new", "export:in_work")


@pytest.mark.asyncio
async def test_admin_only_middleware():
    """Test that AdminOnlyMiddleware allows admin and drops other users."""
    middleware = AdminOnlyMiddleware()
    handler = AsyncMock(return_value="success")

    admin_user = User(id=settings.ADMIN_TG_ID, is_bot=False, first_name="Admin")
    other_user = User(id=99999999, is_bot=False, first_name="Stranger")

    # Case 1: Admin message allowed
    msg_admin = MagicMock(spec=Message)
    msg_admin.from_user = admin_user
    res = await middleware(handler, msg_admin, {"event_from_user": admin_user})
    assert res == "success"
    handler.assert_awaited_once()

    handler.reset_mock()

    # Case 2: Non-admin message blocked (returns None)
    msg_other = MagicMock(spec=Message)
    msg_other.from_user = other_user
    res = await middleware(handler, msg_other, {"event_from_user": other_user})
    assert res is None
    handler.assert_not_awaited()

    handler.reset_mock()

    # Case 3: Admin callback query allowed
    cb_admin = MagicMock(spec=CallbackQuery)
    cb_admin.from_user = admin_user
    res = await middleware(handler, cb_admin, {"event_from_user": admin_user})
    assert res == "success"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_handlers_navigation():
    """Test start, search menu, and district selection handlers."""
    from src.bot.handlers.search import handle_dist_back, handle_district_chosen, handle_search_menu, handle_start

    # /start
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    await handle_start(msg)
    msg.answer.assert_awaited_once()
    assert "SPb Lead Finder" in msg.answer.call_args[0][0]

    # Search menu
    msg.answer.reset_mock()
    await handle_search_menu(msg)
    msg.answer.assert_awaited_once()
    assert "Выберите район" in msg.answer.call_args[0][0]

    # District back button
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    await handle_dist_back(cb)
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()

    # District chosen
    cb.data = "dist:0"
    cb.answer.reset_mock()
    cb.message.edit_text.reset_mock()
    await handle_district_chosen(cb)
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    assert "Центральный" in cb.message.edit_text.call_args[0][0]



class _DummySessionCtx:
    """Async context manager standing in for async_session_maker() in handler tests."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_category_harvest_error_text_is_html_escaped(monkeypatch: pytest.MonkeyPatch):
    """Exception text with '<' or '&' must be escaped, otherwise Telegram rejects the error report."""
    from src.bot.handlers import search

    async def failing_harvest(**kwargs):
        raise ValueError("Unsupported lead_data type: <class 'dict'> & more")

    monkeypatch.setattr(search, "run_lead_harvest", failing_harvest)
    monkeypatch.setattr(search, "async_session_maker", _DummySessionCtx)

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "cat:0:0"
    cb.answer = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()

    await search.handle_category_chosen(cb)

    report = cb.message.edit_text.call_args[0][0]
    assert "<class" not in report
    assert "&lt;class &#x27;dict&#x27;&gt; &amp; more" in report


@pytest.mark.asyncio
async def test_handlers_survive_inaccessible_message():
    """Buttons under messages older than 48h arrive as InaccessibleMessage, which has no edit_text."""
    from aiogram.types import Chat, InaccessibleMessage

    from src.bot.handlers.leads import _reply
    from src.bot.handlers.search import handle_district_chosen

    old_msg = InaccessibleMessage(chat=Chat(id=1, type="private"), message_id=10)

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "dist:0"
    cb.answer = AsyncMock()
    cb.message = old_msg

    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(InaccessibleMessage, "answer", fake_answer)
        await handle_district_chosen(cb)
        await _reply(cb, "card text", edit=True)

    assert any("Центральный" in text for text in sent)
    assert "card text" in sent
