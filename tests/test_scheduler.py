"""Tests for running collections: no overlaps, surviving failures, topping up the pool, shutdown."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.config import Settings
from src.database.models import CollectorState, Lead
from src.database.repo import get_collector_state
from src.services import pipeline, scheduler
from src.services.api_client import ProviderAuthError, ProviderTemporaryError
from src.services.collector import load_collector_config
from src.services.providers.base import BaseMapProvider, RawPlace, SearchPage
from src.services.providers.registry import ProviderSet
from src.services.scheduler import (
    COLLECT_JOB_ID,
    MISFIRE_GRACE_SECONDS,
    POOL_CHECK_JOB_ID,
    STATUS_DAILY_LIMIT,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_OK,
    STATUS_PARTIAL,
    TRIGGER_BATCH,
    TRIGGER_LOW_POOL,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    CollectorRunner,
    RunReport,
    TopupOutcome,
)


class Provider(BaseMapProvider):
    """Returns one new place per request; can block until released, or fail."""

    name = "fake"

    def __init__(self, error: Exception | None = None, block: bool = False) -> None:
        self.error = error
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        number = self.calls
        return SearchPage(places=[
            RawPlace(
                external_id=f"fake_{number}",
                source="fake",
                name=f"Организация {number}",
                category="dentist_clinic",
                phone=f"+7 812 555-{number:02d}-{number:02d}",
            )
        ])


@pytest.fixture(autouse=True)
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


class Reports:
    """Report callback that remembers what it received."""

    def __init__(self) -> None:
        self.items: list[RunReport] = []
        self.received = asyncio.Event()

    async def __call__(self, report: RunReport) -> None:
        self.items.append(report)
        self.received.set()


def _runner(session_maker, provider: BaseMapProvider | None, reports: Reports, **kwargs) -> CollectorRunner:
    settings = {"COLLECT_QUERIES": "dentist_clinic", "COLLECT_DISTRICTS": "Центральный"}
    settings.update(kwargs.pop("settings", {}))
    config = load_collector_config(Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, **settings))
    provider_set = ProviderSet(providers=[provider] if provider else [], disabled=kwargs.pop("disabled", {}))
    return CollectorRunner(session_maker, provider_set, config, reports, **kwargs)


async def _state(session_maker) -> CollectorState:
    async with session_maker() as session:
        state = await get_collector_state(session)
        await session.commit()
    return state


async def _set_state(session_maker, **values) -> None:
    async with session_maker() as session:
        state = await get_collector_state(session)
        for name, value in values.items():
            setattr(state, name, value)
        await session.commit()


async def test_second_run_does_not_start_while_one_is_running(session_maker) -> None:
    provider = Provider(block=True)
    reports = Reports()
    runner = _runner(session_maker, provider, reports)

    first = asyncio.create_task(runner.run_once(TRIGGER_SCHEDULE))
    await provider.started.wait()
    assert runner.is_running

    assert await runner.run_once(TRIGGER_SCHEDULE) is False
    assert runner.start_in_background(TRIGGER_MANUAL) is False
    assert (await runner.request_topup()).outcome is TopupOutcome.RUNNING

    provider.release.set()
    assert await first is True
    assert provider.calls == 1
    assert len(reports.items) == 1
    assert not runner.is_running


async def test_run_saves_its_result_and_reports_new_leads(session_maker) -> None:
    reports = Reports()
    runner = _runner(session_maker, Provider(), reports)
    assert await runner.run_once(TRIGGER_SCHEDULE) is True

    state = await _state(session_maker)
    assert (state.last_status, state.last_trigger) == (STATUS_OK, TRIGGER_SCHEDULE)
    assert (state.found, state.saved_new, state.errors) == (1, 1, 0)
    assert state.last_started_at <= state.last_finished_at
    report = reports.items[0]
    assert report.announce is True
    assert report.pool_size == 1


async def test_nothing_new_is_silent_on_schedule_but_reported_when_someone_waits(session_maker) -> None:
    class NothingFound(Provider):
        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            return SearchPage(places=[])

    for trigger, expected in (
        (TRIGGER_SCHEDULE, False), (TRIGGER_LOW_POOL, False), (TRIGGER_MANUAL, True), (TRIGGER_BATCH, True),
    ):
        reports = Reports()
        await _runner(session_maker, NothingFound(), reports).run_once(trigger)
        assert reports.items[0].announce is expected, trigger


async def test_failure_of_an_iteration_does_not_stop_later_runs(session_maker, monkeypatch) -> None:
    reports = Reports()
    runner = _runner(session_maker, Provider(), reports)

    async def explode(*args, **kwargs):
        raise RuntimeError("disk is full")

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "collect_leads", explode)
        assert await runner.run_once(TRIGGER_SCHEDULE) is True

    state = await _state(session_maker)
    assert state.last_status == STATUS_FAILED
    assert "disk is full" not in (state.last_error or "")  # details stay in the log
    assert reports.items[0].new_alerts

    assert await runner.run_once(TRIGGER_SCHEDULE) is True
    assert (await _state(session_maker)).last_status == STATUS_OK


async def test_error_in_the_report_callback_does_not_break_the_run(session_maker) -> None:
    async def broken_report(report: RunReport) -> None:
        raise RuntimeError("telegram is down")

    runner = _runner(session_maker, Provider(), broken_report)
    assert await runner.run_once(TRIGGER_SCHEDULE) is True
    assert not runner.is_running
    assert (await _state(session_maker)).saved_new == 1


async def test_same_problem_is_alerted_once_and_again_after_it_is_gone(session_maker) -> None:
    failing = Provider(error=ProviderAuthError("2ГИС: код 403. Проверьте ключ"))
    reports = Reports()

    runner = _runner(session_maker, failing, reports)
    await runner.run_once(TRIGGER_SCHEDULE)
    await runner.run_once(TRIGGER_SCHEDULE)
    assert [len(report.new_alerts) for report in reports.items] == [1, 0]
    assert (await _state(session_maker)).last_status == STATUS_FAILED

    failing.error = None
    await runner.run_once(TRIGGER_SCHEDULE)
    assert (await _state(session_maker)).last_alert is None

    failing.error = ProviderAuthError("2ГИС: код 403. Проверьте ключ")
    await runner.run_once(TRIGGER_SCHEDULE)
    assert len(reports.items[-1].new_alerts) == 1


async def test_partial_status_when_some_targets_failed(session_maker) -> None:
    class SecondFails(Provider):
        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            if self.calls == 1:
                self.calls += 1
                raise ProviderTemporaryError("сеть")
            return await super().search(query, area, page, page_size)

    settings = {"COLLECT_QUERIES": "dentist_clinic,beauty"}
    await _runner(session_maker, SecondFails(), Reports(), settings=settings).run_once(TRIGGER_SCHEDULE)
    assert (await _state(session_maker)).last_status == STATUS_PARTIAL


async def test_no_key_and_no_other_source_gives_a_clear_message_without_requests(session_maker) -> None:
    reports = Reports()
    runner = _runner(session_maker, None, reports, disabled={"2gis": "не задан TWOGIS_API_KEY"})
    assert await runner.run_once(TRIGGER_MANUAL) is True

    state = await _state(session_maker)
    assert state.last_status == STATUS_FAILED
    assert "не задан TWOGIS_API_KEY" in state.last_error
    assert "не задан TWOGIS_API_KEY" in reports.items[0].new_alerts[0]


async def test_daily_limit_stops_runs_and_resets_the_next_day(session_maker) -> None:
    provider = Provider()
    reports = Reports()
    runner = _runner(session_maker, provider, reports, settings={"COLLECT_MAX_REQUESTS_PER_DAY": 1})

    await runner.run_once(TRIGGER_SCHEDULE)
    assert provider.calls == 1
    await _set_state(session_maker, last_started_at=datetime.now(UTC) - timedelta(hours=2))

    await runner.run_once(TRIGGER_SCHEDULE)
    assert provider.calls == 1
    assert (await _state(session_maker)).last_status == STATUS_DAILY_LIMIT
    assert (await runner.request_topup()).outcome is TopupOutcome.DAILY_LIMIT

    await _set_state(session_maker, requests_day="2000-01-01")
    await runner.run_once(TRIGGER_SCHEDULE)
    assert provider.calls == 2


async def test_budget_is_limited_by_what_is_left_of_the_day(session_maker) -> None:
    provider = Provider()
    settings = {
        "COLLECT_QUERIES": "dentist_clinic,beauty,hotels",
        "COLLECT_MAX_REQUESTS_PER_DAY": 12,
    }
    runner = _runner(session_maker, provider, Reports(), settings=settings)
    await _set_state(session_maker, requests_day=runner._today(), requests_used=10)
    await runner.run_once(TRIGGER_SCHEDULE)
    assert provider.calls == 2
    assert (await _state(session_maker)).requests_used == 12


async def test_low_pool_starts_extra_collection_at_most_once_an_hour(session_maker) -> None:
    provider = Provider()
    reports = Reports()
    runner = _runner(session_maker, provider, reports, min_pool=60)

    await runner.check_pool()
    await reports.received.wait()
    assert provider.calls == 1
    assert (await _state(session_maker)).last_trigger == TRIGGER_LOW_POOL
    await asyncio.sleep(0)  # the background task finishes

    await runner.check_pool()
    assert provider.calls == 1

    decision = await runner.request_topup()
    assert decision.outcome is TopupOutcome.COOLDOWN
    started = (await _state(session_maker)).last_started_at
    assert decision.retry_at == started + timedelta(hours=1)

    reports.received.clear()
    await _set_state(session_maker, last_started_at=datetime.now(UTC) - timedelta(minutes=61))
    await runner.check_pool()
    await reports.received.wait()
    assert provider.calls == 2


async def test_enough_leads_in_the_pool_means_no_extra_collection(session_maker) -> None:
    provider = Provider()
    runner = _runner(session_maker, provider, Reports(), min_pool=1)
    async with session_maker() as session:
        session.add(Lead(external_id="a", source="x", name="n", category="c", site_status="no_site"))
        await session.commit()
    await runner.check_pool()
    assert provider.calls == 0 and not runner.is_running


async def test_zero_threshold_disables_pool_checks(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports(), min_pool=0)
    await runner.check_pool()
    assert not runner.is_running


async def test_switched_off_means_no_automatic_collection_but_manual_still_works(session_maker) -> None:
    provider = Provider()
    reports = Reports()
    runner = _runner(session_maker, provider, reports, min_pool=60)
    await runner.set_auto_enabled(False)

    await runner.check_pool()
    assert (await runner.request_topup()).outcome is TopupOutcome.AUTO_OFF
    assert provider.calls == 0

    assert runner.start_in_background(TRIGGER_MANUAL) is True
    await reports.received.wait()
    assert provider.calls == 1


async def test_scheduler_job_has_overlap_protection_and_stops_with_the_bot(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports(), min_pool=60)
    await runner.start()
    try:
        job = runner._scheduler.get_job(COLLECT_JOB_ID)
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (1, True, MISFIRE_GRACE_SECONDS)
        assert job.next_run_time.hour == 4
        assert runner._scheduler.get_job(POOL_CHECK_JOB_ID) is not None
    finally:
        await runner.shutdown()
    assert not runner._scheduler.running


async def test_pool_check_job_is_not_added_when_disabled(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports(), min_pool=0)
    await runner.start()
    try:
        assert runner._scheduler.get_job(POOL_CHECK_JOB_ID) is None
    finally:
        await runner.shutdown()


async def test_run_on_start_fires_through_the_scheduler(session_maker) -> None:
    provider = Provider()
    reports = Reports()
    runner = _runner(session_maker, provider, reports, run_on_start=True)
    await runner.start()
    try:
        await asyncio.wait_for(reports.received.wait(), timeout=5)
    finally:
        await runner.shutdown()
    assert provider.calls == 1
    assert (await _state(session_maker)).last_trigger == TRIGGER_SCHEDULE


async def test_no_run_on_start_by_default(session_maker) -> None:
    provider = Provider()
    runner = _runner(session_maker, provider, Reports())
    await runner.start()
    await asyncio.sleep(0.05)
    await runner.shutdown()
    assert provider.calls == 0


async def test_switch_survives_restart_and_pauses_the_job(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports())
    await runner.start()
    await runner.set_auto_enabled(False)
    status = await runner.get_status()
    assert status.auto_enabled is False and status.next_run_at is None
    await runner.set_auto_enabled(True)
    assert (await runner.get_status()).next_run_at is not None
    await runner.set_auto_enabled(False)
    await runner.shutdown()

    restarted = _runner(session_maker, Provider(), Reports(), run_on_start=True)
    await restarted.start()
    status = await restarted.get_status()
    await restarted.shutdown()
    assert status.auto_enabled is False and status.next_run_at is None


async def test_setting_disables_automatic_runs_until_switched_on(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports(), enabled_by_default=False)
    await runner.start()
    status = await runner.get_status()
    await runner.shutdown()
    assert status.auto_enabled is False and status.next_run_at is None


async def test_shutdown_waits_for_a_run_that_finishes_in_time(session_maker) -> None:
    provider = Provider(block=True)
    runner = _runner(session_maker, provider, Reports())
    await runner.start()
    runner.start_in_background(TRIGGER_MANUAL)
    await provider.started.wait()

    asyncio.get_running_loop().call_later(0.01, provider.release.set)
    await runner.shutdown(wait_seconds=5)
    assert (await _state(session_maker)).last_status == STATUS_OK


async def test_shutdown_cancels_a_long_run_and_records_it(session_maker) -> None:
    provider = Provider(block=True)
    runner = _runner(session_maker, provider, Reports())
    await runner.start()
    runner.start_in_background(TRIGGER_MANUAL)
    await provider.started.wait()

    await runner.shutdown(wait_seconds=0.01)

    assert not runner.is_running
    assert (await _state(session_maker)).last_status == STATUS_INTERRUPTED


async def test_run_killed_with_the_process_is_marked_on_next_start(session_maker) -> None:
    await _set_state(session_maker, last_status="running")
    runner = _runner(session_maker, Provider(), Reports())
    await runner.start()
    await runner.shutdown()
    assert (await _state(session_maker)).last_status == STATUS_INTERRUPTED


async def test_status_shows_sources_and_schedule(session_maker) -> None:
    runner = _runner(session_maker, Provider(), Reports(), disabled={"2gis": "не задан TWOGIS_API_KEY"})
    status = await runner.get_status()
    assert status.active_providers == ["fake"]
    assert status.disabled_providers == {"2gis": "не задан TWOGIS_API_KEY"}
    assert "0 4 * * *" in status.schedule_text
    assert "Europe/Moscow" in status.schedule_text
