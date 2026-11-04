"""Tests for validation of inline button callback data (forged, malformed or stale buttons)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from src.bot.handlers import leads as leads_handlers
from src.bot.handlers import search
from src.bot.keyboards import STALE_BUTTON_TEXT, get_categories_keyboard, get_districts_keyboard
from src.services.providers.base import SPB_DISTRICTS, TARGET_CATEGORIES


def _callback(data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.message.answer_document = AsyncMock()
    return cb


def _assert_rejected_as_stale(cb: MagicMock) -> None:
    cb.answer.assert_awaited_once_with(STALE_BUTTON_TEXT, show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    cb.message.answer.assert_not_awaited()


class _FailIfOpened:
    """async_session_maker replacement: invalid buttons must not reach the database."""

    def __call__(self):
        raise AssertionError("database must not be touched for an invalid button")


def test_keyboards_use_stable_keys_not_positions() -> None:
    """Callback data must not depend on dict order, so reordering districts cannot retarget old buttons."""
    district_data = {btn.callback_data for row in get_districts_keyboard().inline_keyboard for btn in row}
    assert district_data == {f"dist:{name}" for name in SPB_DISTRICTS}

    for district in SPB_DISTRICTS:
        cat_data = {
            btn.callback_data
            for row in get_categories_keyboard(district).inline_keyboard
            for btn in row
            if btn.callback_data != "dist_back"
        }
        assert cat_data == {f"cat:{district}:{key}" for key in TARGET_CATEGORIES}
        assert all(len(data.encode("utf-8")) <= 64 for data in cat_data)


@pytest.mark.asyncio
async def test_district_button_shows_categories_for_that_district() -> None:
    cb = _callback("dist:Невский")
    await search.handle_district_chosen(cb)
    cb.answer.assert_awaited_once_with()
    text = cb.message.edit_text.call_args[0][0]
    assert "Невский" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["dist:-1", "dist:0", "dist:Атлантида", "dist:"])
async def test_invalid_or_stale_district_button_is_rejected(data: str) -> None:
    """'dist:-1' used to wrap around to the last district; old index buttons must not be misread."""
    cb = _callback(data)
    await search.handle_district_chosen(cb)
    _assert_rejected_as_stale(cb)


@pytest.mark.asyncio
async def test_category_button_harvests_selected_district_and_category(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_harvest(**kwargs):
        calls.append(kwargs)
        return {"total_found": 0, "qualified": 0, "saved_new": 0, "duplicates_skipped": 0}

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(search, "run_lead_harvest", fake_harvest)
    monkeypatch.setattr(search, "async_session_maker", _Session)

    await search.handle_category_chosen(_callback("cat:Невский:hotels"))

    assert (calls[0]["district_name"], calls[0]["category_key"]) == ("Невский", "hotels")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["cat:0:0", "cat:-1:-1", "cat:Невский:unknown", "cat:Атлантида:hotels", "cat:Невский", "cat:"],
)
async def test_invalid_or_stale_category_button_is_rejected(monkeypatch: pytest.MonkeyPatch, data: str) -> None:
    async def fail_harvest(**kwargs):
        raise AssertionError("harvest must not start for an invalid button")

    monkeypatch.setattr(search, "run_lead_harvest", fail_harvest)
    cb = _callback(data)
    await search.handle_category_chosen(cb)
    _assert_rejected_as_stale(cb)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["lead:abc:work:0", "lead:1:delete:0", "lead:1:next:-1", "lead:1:next", "lead:1:next:0:extra", "lead:"],
)
async def test_invalid_lead_button_is_rejected(monkeypatch: pytest.MonkeyPatch, data: str) -> None:
    monkeypatch.setattr(leads_handlers, "async_session_maker", _FailIfOpened())
    cb = _callback(data)
    await leads_handlers.handle_lead_action(cb)
    _assert_rejected_as_stale(cb)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["export:", "export:deleted", "export:new:extra"])
async def test_invalid_export_button_is_rejected(monkeypatch: pytest.MonkeyPatch, data: str) -> None:
    monkeypatch.setattr(leads_handlers, "async_session_maker", _FailIfOpened())
    cb = _callback(data)
    await leads_handlers.handle_export_csv(cb)
    cb.answer.assert_awaited_once_with(STALE_BUTTON_TEXT, show_alert=True)
    cb.message.answer_document.assert_not_awaited()
