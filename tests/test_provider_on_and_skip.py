"""/provider_on resets only the pause that is there; the report of a run where every provider was skipped."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.formatters import format_collection_report
from src.bot.handlers import collector as handlers
from src.database.models import ProviderState
from src.database.repo import ProviderReset, get_provider_states, reset_provider_pause
from src.services.collector import CollectResult

NAME = "2gis_cdp"
LATER = datetime.now(UTC) + timedelta(hours=5)


async def _put_state(session_maker, **fields) -> None:
    async with session_maker() as session:
        session.add(ProviderState(name=NAME, **{"consecutive_blocks": 0, **fields}))
        await session.commit()


async def _state(session_maker) -> ProviderState:
    async with session_maker() as session:
        return (await get_provider_states(session))[NAME]


async def test_only_blocks_are_reset(session_maker) -> None:
    await _put_state(session_maker, consecutive_blocks=2, blocked_until=LATER)
    async with session_maker() as session:
        outcome = await reset_provider_pause(session, NAME)
    assert (outcome.blocks_reset, outcome.blocks_before, outcome.network_reset) == (True, 2, False)
    state = await _state(session_maker)
    assert state.consecutive_blocks == 0 and state.blocked_until is None and state.disabled_at is None


async def test_only_the_network_series_is_reset(session_maker) -> None:
    await _put_state(session_maker, network_failure_streak=3, network_paused_until=LATER)
    async with session_maker() as session:
        outcome = await reset_provider_pause(session, NAME)
    assert (outcome.blocks_reset, outcome.network_reset, outcome.network_before) == (False, True, 3)
    state = await _state(session_maker)
    assert state.network_failure_streak == 0 and state.network_paused_until is None


async def test_both_are_reset_when_both_are_active(session_maker) -> None:
    await _put_state(session_maker, consecutive_blocks=3, disabled_at=LATER, network_paused_until=LATER)
    async with session_maker() as session:
        outcome = await reset_provider_pause(session, NAME)
    assert outcome.blocks_reset and outcome.network_reset
    state = await _state(session_maker)
    assert state.disabled_at is None and state.network_paused_until is None


async def test_nothing_paused_changes_nothing(session_maker) -> None:
    await _put_state(session_maker, last_block_reason="old")
    async with session_maker() as session:
        outcome = await reset_provider_pause(session, NAME)
    assert outcome.nothing_paused
    assert (await _state(session_maker)).last_block_reason == "old"


async def test_all_resets_everything_even_a_clean_part(session_maker) -> None:
    await _put_state(session_maker, consecutive_blocks=1, blocked_until=LATER)
    async with session_maker() as session:
        outcome = await reset_provider_pause(session, NAME, everything=True)
    assert outcome.blocks_reset and outcome.network_reset and outcome.everything
    assert not outcome.nothing_paused


async def _run(args: str | None, outcome: ProviderReset) -> tuple[MagicMock, str]:
    runner = MagicMock()
    runner.known_provider = MagicMock(side_effect=lambda name: name == NAME)
    runner.reset_provider_pause = AsyncMock(return_value=outcome)
    message = MagicMock()
    message.answer = AsyncMock()
    await handlers.handle_provider_on(message, MagicMock(args=args), runner)
    return runner, message.answer.await_args.args[0]


async def test_reply_says_what_was_reset_and_what_was_left() -> None:
    runner, text = await _run(NAME, ProviderReset(blocks_reset=True, blocks_before=2))
    runner.reset_provider_pause.assert_awaited_once_with(NAME, everything=False)
    assert "Сброшено: блокировки (было 2 из 3)" in text
    assert "Не тронуто: сетевая серия" in text


async def test_reply_for_the_network_series() -> None:
    _, text = await _run(NAME, ProviderReset(network_reset=True, network_before=3))
    assert "Сброшено: сетевая серия (было 3)" in text
    assert "Не тронуто: блокировки" in text


async def test_all_argument_is_passed_on() -> None:
    runner, text = await _run(f"{NAME} ALL", ProviderReset(True, 0, True, 0, True))
    runner.reset_provider_pause.assert_awaited_once_with(NAME, everything=True)
    assert "Не тронуто: ничего" in text


async def test_nothing_paused_is_said_plainly() -> None:
    _, text = await _run(NAME, ProviderReset())
    assert "ничего не менял" in text


@pytest.mark.parametrize("args", [f"{NAME} everything", f"{NAME} all now", f"{NAME} 1 2"])
async def test_unknown_extra_argument_gets_usage_and_changes_nothing(args: str) -> None:
    runner, text = await _run(args, ProviderReset())
    runner.reset_provider_pause.assert_not_awaited()
    assert "Формат:" in text


def _report(result: CollectResult, status: str = "ok") -> MagicMock:
    return MagicMock(status=status, result=result, duration_seconds=1.0, pool_size=5)


def test_all_skipped_run_has_its_own_header_and_no_zero_lines() -> None:
    result = CollectResult(skipped={"2gis_cdp": "пауза после блокировки", "2gis<x>": "отключён"})
    text = format_collection_report(_report(result))
    assert text.startswith("⏭ <b>Сбор пропущен</b>")
    assert "Сбор лидов завершён" not in text
    assert "⏭ 2gis_cdp: пауза после блокировки" in text
    assert "2gis&lt;x&gt;: отключён" in text
    for word in ("Найдено", "Новых", "Ошибок", "Страниц", "Карточек", "Длительность"):
        assert word not in text


def test_mixed_run_keeps_the_normal_format() -> None:
    result = CollectResult(found=2, requests=3, skipped={"osm": "пауза"})
    text = format_collection_report(_report(result))
    assert "Сбор лидов завершён" in text
    assert "Найдено: <b>2</b>" in text
    assert "⏭ osm пропущен: пауза" in text


def test_normal_run_keeps_the_normal_format() -> None:
    text = format_collection_report(_report(CollectResult(found=1, requests=1)))
    assert "🏁 <b>Сбор лидов завершён</b> (успешно)" in text


def test_a_run_with_errors_and_skips_is_not_called_skipped() -> None:
    result = CollectResult(errors=1, last_error="boom", skipped={"osm": "пауза"})
    assert "Сбор лидов завершён" in format_collection_report(_report(result, "failed"))
