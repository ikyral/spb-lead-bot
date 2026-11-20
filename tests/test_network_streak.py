"""Runs that end with a network error in a row: a persisted counter and a separate 6-hour pause."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.bot.formatters import _provider_line
from src.config import Settings
from src.database.models import ProviderState
from src.database.repo import enable_provider, get_provider_states, init_db
from src.services import pipeline
from src.services.api_client import (
    ProviderBlockedError,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderProxyError,
)
from src.services.collector import (
    NETWORK_FAILURE_PAUSE_HOURS,
    NETWORK_FAILURE_RUNS_BEFORE_PAUSE,
    collect_leads,
    load_collector_config,
    unavailable_reason,
)
from src.services.providers.base import BaseMapProvider, RawPlace, SearchPage

NAME = "2gis_cdp"
PAUSE_ALERT = "запусков подряд закончились сетевой ошибкой"


class Scripted(BaseMapProvider):
    """Answers each search call with the next prepared outcome: a list of places or an error to raise."""

    def __init__(self, outcomes: list, name: str = NAME) -> None:
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else []
        if isinstance(outcome, Exception):
            raise outcome
        return SearchPage(places=outcome, has_more=False)


def _config():
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="t",
        ADMIN_TG_ID=1,
        COLLECT_QUERIES="dentist_clinic,barbershop,tire_service",
        COLLECT_DISTRICTS="Центральный",
    )
    return load_collector_config(settings)


def _place(number: int) -> RawPlace:
    return RawPlace(
        external_id=f"2gis_{number}",
        source="2gis",
        name=f"Организация {number}",
        category="dentist_clinic",
        phone=f"+7 812 555-{number:02d}-{number:02d}",
        high_margin=True,
    )


@pytest.fixture(autouse=True)
def no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


async def _state(session_maker, name: str = NAME) -> ProviderState | None:
    async with session_maker() as session:
        return (await get_provider_states(session)).get(name)


async def _network_run(session_maker):
    provider = Scripted([ProviderNoConnectionError("ERR_CONNECTION_RESET"), [_place(1)], [_place(2)]])
    result = await collect_leads(session_maker, [provider], _config())
    return provider, result


def _pause_alerts(result) -> list[str]:
    return [alert for alert in result.alerts if PAUSE_ALERT in alert]


async def test_a_network_error_makes_one_request_and_drops_the_provider(session_maker) -> None:
    provider, result = await _network_run(session_maker)
    assert provider.calls == 1  # the next targets were not tried in this run
    assert result.requests == 1
    state = await _state(session_maker)
    assert state is not None and state.network_failure_streak == 1


async def test_the_third_run_in_a_row_pauses_for_six_hours_with_one_alert(session_maker, caplog) -> None:
    for run in range(1, NETWORK_FAILURE_RUNS_BEFORE_PAUSE):
        _, result = await _network_run(session_maker)
        assert _pause_alerts(result) == []
        state = await _state(session_maker)
        assert state is not None
        assert state.network_failure_streak == run
        assert state.network_paused_until is None

    with caplog.at_level("WARNING"):
        _, result = await _network_run(session_maker)
    assert len(_pause_alerts(result)) == 1
    assert "не блокировка" in _pause_alerts(result)[0]
    assert any("paused after network failures" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    state = await _state(session_maker)
    assert state is not None
    assert state.network_failure_streak == 3
    assert state.network_paused_until is not None
    expected = datetime.now(UTC) + timedelta(hours=NETWORK_FAILURE_PAUSE_HOURS)
    assert abs((state.network_paused_until - expected).total_seconds()) < 60
    assert state.consecutive_blocks == 0
    assert state.blocked_until is None
    assert state.last_block_reason is None
    assert state.disabled_at is None


async def test_runs_during_the_pause_are_skipped_and_a_later_failure_pauses_again(session_maker) -> None:
    for _ in range(NETWORK_FAILURE_RUNS_BEFORE_PAUSE):
        await _network_run(session_maker)

    provider, result = await _network_run(session_maker)
    assert provider.calls == 0
    assert "сетевых сбоев" in result.skipped[NAME]
    state = await _state(session_maker)
    assert state is not None and state.network_failure_streak == 3

    # The pause is over: the next network error pauses again right away.
    async with session_maker() as session:
        stored = await session.get(ProviderState, NAME)
        assert stored is not None
        stored.network_paused_until = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    provider, result = await _network_run(session_maker)
    assert provider.calls == 1
    assert len(_pause_alerts(result)) == 1
    state = await _state(session_maker)
    assert state is not None
    assert state.network_failure_streak == 4
    assert state.network_paused_until is not None and state.network_paused_until > datetime.now(UTC)
    assert state.consecutive_blocks == 0
    assert state.blocked_until is None


async def test_a_received_page_resets_the_streak(session_maker) -> None:
    await _network_run(session_maker)
    await _network_run(session_maker)
    provider = Scripted([[_place(1)]])
    await collect_leads(session_maker, [provider], _config())
    state = await _state(session_maker)
    assert state is not None and state.network_failure_streak == 0

    _, result = await _network_run(session_maker)
    assert _pause_alerts(result) == []
    state = await _state(session_maker)
    assert state is not None and state.network_failure_streak == 1


async def test_a_page_then_a_network_error_in_one_run_does_not_count(session_maker) -> None:
    await _network_run(session_maker)
    provider = Scripted([[_place(1)], ProviderNoConnectionError("ERR_EMPTY_RESPONSE")])
    await collect_leads(session_maker, [provider], _config())
    state = await _state(session_maker)
    assert state is not None and state.network_failure_streak == 0


async def test_the_streak_survives_a_restart(session_maker, tmp_path: Path) -> None:
    await _network_run(session_maker)
    await _network_run(session_maker)

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    try:
        restarted = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        state = await _state(restarted)
        assert state is not None and state.network_failure_streak == 2
        _, result = await _network_run(restarted)
        assert len(_pause_alerts(result)) == 1
        state = await _state(restarted)
        assert state is not None and state.network_paused_until is not None
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "error",
    [ProviderProxyError("прокси не отвечает"), ProviderBlockedError("капча"), ProviderFormatError("формат")],
)
async def test_proxy_block_and_format_errors_do_not_count(session_maker, error) -> None:
    for _ in range(NETWORK_FAILURE_RUNS_BEFORE_PAUSE):
        async with session_maker() as session:
            # A block pauses the provider; lift it so every run really tries.
            stored = await session.get(ProviderState, NAME)
            if stored is not None:
                stored.blocked_until = None
                stored.disabled_at = None
                await session.commit()
        result = await collect_leads(session_maker, [Scripted([error, error, error])], _config())
        assert _pause_alerts(result) == []
    state = await _state(session_maker)
    assert state is None or state.network_failure_streak == 0
    assert state is None or state.network_paused_until is None


async def test_one_run_counts_once(session_maker) -> None:
    # The provider is dropped after the first network error, so a second one never comes in the same run.
    provider = Scripted([ProviderNoConnectionError("1"), ProviderNoConnectionError("2")])
    await collect_leads(session_maker, [provider], _config())
    state = await _state(session_maker)
    assert provider.calls == 1
    assert state is not None and state.network_failure_streak == 1


async def test_manual_enabling_clears_the_network_pause(session_maker) -> None:
    for _ in range(NETWORK_FAILURE_RUNS_BEFORE_PAUSE):
        await _network_run(session_maker)
    async with session_maker() as session:
        await enable_provider(session, NAME)
    state = await _state(session_maker)
    assert state is not None
    assert state.network_failure_streak == 0
    assert state.network_paused_until is None


def test_the_pause_is_explained_where_the_block_pause_is() -> None:
    now = datetime.now(UTC)
    state = ProviderState(name=NAME, consecutive_blocks=0, network_paused_until=now + timedelta(hours=6))
    provider = Scripted([])
    reason = unavailable_reason(provider, state, now)
    assert reason is not None and reason.startswith("пауза после повторных сетевых сбоев (до ")
    assert "сетевых сбоев" in _provider_line(NAME, state, now)
    assert unavailable_reason(provider, state, now + timedelta(hours=7)) is None


async def test_old_databases_get_the_network_columns(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE provider_state (name VARCHAR(32) PRIMARY KEY, consecutive_blocks INTEGER)")
        )
        await conn.execute(text("INSERT INTO provider_state (name, consecutive_blocks) VALUES ('2gis_cdp', 1)"))
    await init_db(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT network_failure_streak, network_paused_until FROM provider_state"))
        ).one()
        columns = {
            column[1]: column for column in (await conn.execute(text("PRAGMA table_info(provider_state)"))).all()
        }
    await engine.dispose()

    assert tuple(row) == (0, None)
    assert columns["network_failure_streak"][3] == 1  # NOT NULL
    assert columns["network_failure_streak"][4] == "0"
