"""Tests for the collector on a separate computer and for a server that does not collect by itself."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import main as bot_main
from src import collector_cli
from src.bot.formatters import format_collector_status
from src.bot.handlers import collector as collector_handlers
from src.bot.handlers import issue
from src.bot.notifier import AdminNotifier
from src.config import PROJECT_ROOT, Settings, resolve_path
from src.database.models import CollectorState, ImportLog, Lead, ProviderState
from src.database.repo import MAX_CONSECUTIVE_BLOCKS, get_collector_state, get_provider_states
from src.services import pipeline
from src.services.api_client import ProviderBlockedError
from src.services.collector import load_collector_config
from src.services.lead_file import parse_lead_file
from src.services.providers.base import BaseMapProvider, RawPlace, SearchPage
from src.services.providers.registry import ProviderSet
from src.services.scheduler import COLLECT_JOB_ID, POOL_CHECK_JOB_ID, CollectorRunner, TopupDecision, TopupOutcome


class Provider(BaseMapProvider):
    """Stands in for the scraper: no browser, no network."""

    name = "2gis_scraper"

    def __init__(self, error: Exception | None = None, min_hours: float = 0.0) -> None:
        self.error = error
        self.calls = 0
        self.closed = 0
        self.min_hours_between_runs = min_hours

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SearchPage(
            places=[
                RawPlace(
                    external_id=f"2gis_{self.calls}",
                    source="2gis",
                    name=f"Организация {self.calls}",
                    category="dentist_clinic",
                    phone=f"+7 812 555-{self.calls:02d}-00",
                    source_url=f"https://2gis.ru/firm/{self.calls}",
                )
            ]
        )

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Runs the command line with settings for a temporary folder and a chosen provider."""
    settings = Settings(
        _env_file=None,
        COLLECTOR_DB_PATH=str(tmp_path / "data" / "collector.db"),
        COLLECTOR_EXPORT_DIR=str(tmp_path / "exports"),
        COLLECT_QUERIES="dentist_clinic",
        COLLECT_DISTRICTS="Центральный",
    )
    monkeypatch.setattr(collector_cli, "settings", settings)
    monkeypatch.setattr(collector_cli, "settings_error", None)
    holder = MagicMock()
    holder.provider = Provider()
    holder.exports = tmp_path / "exports"
    holder.settings = settings

    def fake_build(names, key, options=None):
        holder.names, holder.options = names, options
        return ProviderSet(providers=[holder.provider])

    monkeypatch.setattr(collector_cli, "build_providers", fake_build)
    return holder


async def _state(tmp_path: Path, name: str = "2gis_scraper") -> ProviderState:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "data" / "collector.db")))
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        state = (await get_provider_states(session))[name]
    await engine.dispose()
    return state


def _set_state(tmp_path: Path, **values) -> None:
    async def run() -> None:
        engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "data" / "collector.db")))
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            state = await session.get(ProviderState, "2gis_scraper")
            for name, value in values.items():
                setattr(state, name, value)
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_settings_need_no_bot_token() -> None:
    settings = Settings(_env_file=None)
    assert settings.BOT_TOKEN.get_secret_value() == ""
    assert settings.COLLECTOR_PROVIDERS == "2gis_scraper"
    assert settings.SCRAPER_HEADLESS is True


async def test_bot_still_refuses_to_start_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_main, "init_db", AsyncMock(side_effect=AssertionError("must not be reached")))
    monkeypatch.setattr(bot_main, "settings_error", None)
    monkeypatch.setattr(bot_main, "settings", Settings(_env_file=None))
    with pytest.raises(SystemExit, match="BOT_TOKEN and ADMIN_TG_ID must be set"):
        await bot_main.main()


def test_collector_does_not_import_the_bot() -> None:
    code = "import sys, src.collector_cli; print('aiogram' in sys.modules, 'main' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip().splitlines()[-1] == "False False"


def test_real_entry_point_runs_without_a_token(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BOT_TOKEN", "ADMIN_TG_ID", "DB_URL")
    }
    env.update(
        COLLECTOR_DB_PATH=str(tmp_path / "c.db"),
        COLLECTOR_EXPORT_DIR=str(tmp_path / "exports"),
        DB_URL=f"sqlite+aiosqlite:///{(tmp_path / 'must_not_appear.db').as_posix()}",
    )
    result = subprocess.run(
        [sys.executable, "collect_cli.py", "--export-only"],
        cwd=PROJECT_ROOT, capture_output=True, timeout=120, check=False, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Выгружено лидов: 0".encode() in result.stdout
    assert (tmp_path / "c.db").exists()
    assert not (tmp_path / "must_not_appear.db").exists()  # the bot's database stays untouched


def test_paths_from_settings_are_relative_to_the_project() -> None:
    assert resolve_path("data/collector.db") == PROJECT_ROOT / "data" / "collector.db"
    absolute = PROJECT_ROOT / "x"
    assert resolve_path(str(absolute)) == absolute


def test_collect_and_export(cli, capsys) -> None:
    assert collector_cli.main([]) == 0

    assert cli.names == ["2gis_scraper"]  # the default source of the collector
    files = list(cli.exports.glob("leads_*.json"))
    assert len(files) == 1
    parsed = parse_lead_file(files[0].read_bytes(), 1024 * 1024, 100)
    assert [place.external_id for place in parsed.places] == ["2gis_1"]

    out = capsys.readouterr().out
    assert "найдено 1" in out and "подходит 1" in out and "новых 1" in out
    assert "Выгружено лидов: 1" in out
    assert str(files[0]) in out
    assert cli.provider.closed == 1


def test_no_export_keeps_leads_for_later(cli, capsys) -> None:
    assert collector_cli.main(["--no-export"]) == 0
    assert list(cli.exports.glob("*")) == [] if cli.exports.exists() else True
    assert "Выгружено" not in capsys.readouterr().out

    cli.provider.error = AssertionError("--export-only must not collect")
    assert collector_cli.main(["--export-only"]) == 0
    assert cli.provider.calls == 1
    assert len(list(cli.exports.glob("leads_*.json"))) == 1


def test_export_only_needs_no_browser_or_providers(cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("no provider may be built")

    monkeypatch.setattr(collector_cli, "build_providers", must_not_run)
    assert collector_cli.main(["--export-only"]) == 0


def test_options_exclude_each_other() -> None:
    with pytest.raises(SystemExit):
        collector_cli.main(["--no-export", "--export-only"])


def test_second_run_exports_only_the_new(cli) -> None:
    assert collector_cli.main([]) == 0
    assert collector_cli.main(["--export-only"]) == 0
    assert len(list(cli.exports.glob("leads_*.json"))) == 1


def test_captcha_stops_the_source_and_the_exit_code_says_so(cli, capsys, tmp_path: Path) -> None:
    cli.provider = Provider(error=ProviderBlockedError("сайт показал капчу"))

    assert collector_cli.main([]) == 2

    out = capsys.readouterr().out
    assert "капчу" in out
    assert "повтор не раньше" in out
    assert "Не запускайте" in out
    assert cli.provider.calls == 1  # stopped at once
    assert cli.provider.closed == 1
    state = asyncio.run(_state(tmp_path))
    assert state.consecutive_blocks == 1
    assert state.blocked_until > datetime.now(UTC) + timedelta(hours=5)


def test_what_was_found_before_a_block_is_still_exported(cli) -> None:
    found = RawPlace(external_id="2gis_1", source="2gis", name="А", category="dentist_clinic", phone="+78125550100")
    cli.provider = Provider(error=ProviderBlockedError("сайт показал капчу", partial=[found]))
    assert collector_cli.main([]) == 2
    assert len(list(cli.exports.glob("leads_*.json"))) == 1


def test_the_pause_survives_between_launches(cli, capsys) -> None:
    cli.provider = Provider(error=ProviderBlockedError("сайт показал капчу"))
    collector_cli.main([])
    capsys.readouterr()

    assert collector_cli.main([]) == 2

    assert cli.provider.calls == 1  # the second launch did not touch the site
    assert "пауза" in capsys.readouterr().out


def test_three_blocks_switch_off_until_enabled_by_hand(cli, capsys, tmp_path: Path) -> None:
    cli.provider = Provider(error=ProviderBlockedError("сайт показал капчу"))
    for _ in range(MAX_CONSECUTIVE_BLOCKS):
        collector_cli.main([])
        _set_state(tmp_path, blocked_until=datetime.now(UTC) - timedelta(seconds=1))
    assert cli.provider.calls == MAX_CONSECUTIVE_BLOCKS
    capsys.readouterr()

    assert collector_cli.main([]) == 2
    assert cli.provider.calls == MAX_CONSECUTIVE_BLOCKS
    out = capsys.readouterr().out
    assert "--enable-provider 2gis_scraper" in out
    assert "/provider_on" not in out

    assert collector_cli.main(["--enable-provider", "2gis_scraper"]) == 0
    assert cli.provider.calls == MAX_CONSECUTIVE_BLOCKS  # enabling does not collect

    cli.provider.error = None
    assert collector_cli.main([]) == 0
    assert cli.provider.calls > MAX_CONSECUTIVE_BLOCKS


def test_not_more_than_one_run_within_the_limit(cli, capsys) -> None:
    cli.provider = Provider(min_hours=20)
    assert collector_cli.main([]) == 0
    calls = cli.provider.calls
    capsys.readouterr()

    assert collector_cli.main([]) == 0
    assert cli.provider.calls == calls
    assert "уже запускался сегодня" in capsys.readouterr().out


def test_no_usable_source_is_an_error(cli, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        collector_cli,
        "build_providers",
        lambda names, key, options=None: ProviderSet(disabled={"2gis_scraper": "не установлен playwright"}),
    )
    assert collector_cli.main([]) == 1
    assert "не установлен playwright" in capsys.readouterr().out


def test_invalid_settings_are_reported(cli, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(collector_cli, "settings", cli.settings.model_copy(update={"COLLECTOR_PROVIDERS": "google"}))
    assert collector_cli.main([]) == 1
    assert "COLLECT_PROVIDERS" in capsys.readouterr().out


def test_broken_env_is_reported_not_a_traceback(cli, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(collector_cli, "settings_error", ValueError("COLLECT_MAX_PAGES: not an integer"))
    assert collector_cli.main([]) == 1
    assert "COLLECT_MAX_PAGES" in capsys.readouterr().out


def test_interruption_gives_its_own_code(cli, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(collector_cli.asyncio, "run", interrupted)
    assert collector_cli.main([]) == 130


def test_headless_setting_reaches_the_browser(cli, monkeypatch: pytest.MonkeyPatch) -> None:
    assert collector_cli.main(["--no-export"]) == 0
    assert cli.options.headless is True
    monkeypatch.setattr(collector_cli, "settings", cli.settings.model_copy(update={"SCRAPER_HEADLESS": False}))
    assert collector_cli.main(["--no-export"]) == 0
    assert cli.options.headless is False


def test_headless_window_changes_only_that_flag_and_limits_stay() -> None:
    from src.services.providers.twogis_scraper import ScraperOptions

    headed = ScraperOptions.from_settings(Settings(_env_file=None, SCRAPER_HEADLESS=False))
    default = ScraperOptions()
    assert headed.headless is False
    assert headed.min_delay_seconds == default.min_delay_seconds
    assert headed.max_delay_seconds == default.max_delay_seconds
    assert headed.max_pages_per_run == default.max_pages_per_run
    assert headed.min_hours_between_runs == default.min_hours_between_runs


# ---------- a server without its own collection ----------


def _config(**overrides):
    defaults = {"COLLECT_QUERIES": "dentist_clinic", "COLLECT_DISTRICTS": "Центральный"}
    return load_collector_config(Settings(_env_file=None, **{**defaults, **overrides}))


def _external_runner(session_maker, provider=None, reminder=None, min_pool: int = 60) -> CollectorRunner:
    return CollectorRunner(
        session_maker,
        ProviderSet(providers=[provider] if provider else []),
        _config(),
        AsyncMock(),
        min_pool=min_pool,
        external_mode=True,
        pool_reminder=reminder,
    )


def test_empty_provider_list_is_different_from_not_set() -> None:
    assert Settings(_env_file=None).COLLECT_PROVIDERS == "osm"
    assert _config(COLLECT_PROVIDERS="").providers == []


@pytest.mark.parametrize(
    "name", ["COLLECT_INTERVAL_MINUTES", "LEADS_MIN_POOL", "COLLECT_ENABLED", "DB_URL", "ADMIN_TG_ID"]
)
def test_other_blank_settings_still_mean_the_default(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "")
    Settings(_env_file=None)


def test_blank_preferred_key_does_not_hide_the_old_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGIS_API_KEY", "")
    monkeypatch.setenv("TWOGIS_API_KEY", "ключ")
    assert Settings(_env_file=None).TWOGIS_API_KEY.get_secret_value() == "ключ"


async def test_no_collection_jobs_and_no_runs_in_external_mode(session_maker) -> None:
    provider = Provider()
    runner = _external_runner(session_maker, provider)
    await runner.start()
    try:
        assert runner._scheduler.get_job(COLLECT_JOB_ID) is None
        assert runner._scheduler.get_job(POOL_CHECK_JOB_ID) is not None  # still asks for files
        assert runner.start_in_background("manual") is False
        assert await runner.run_once("schedule") is False
        await asyncio.sleep(0)
        assert provider.calls == 0
        assert (await runner.get_status()).next_run_at is None
    finally:
        await runner.shutdown()


async def test_external_mode_ignores_a_switch_saved_earlier(session_maker) -> None:
    """COLLECT_ENABLED=false wins over /collect_on from the past."""
    async with session_maker() as session:
        state = await get_collector_state(session)
        state.auto_enabled = True
        await session.commit()
    provider = Provider()
    runner = _external_runner(session_maker, provider)
    await runner.start()
    await runner.shutdown()
    assert provider.calls == 0


async def test_low_pool_asks_for_a_file_once_a_day_instead_of_collecting(session_maker) -> None:
    provider = Provider()
    reminder = AsyncMock()
    runner = _external_runner(session_maker, provider, reminder)

    await runner.check_pool()
    await runner.check_pool()
    reminder.assert_awaited_once_with(0)
    assert provider.calls == 0
    assert not runner.is_running

    async with session_maker() as session:
        state = await get_collector_state(session)
        state.last_pool_reminder_at = datetime.now(UTC) - timedelta(hours=25)
        await session.commit()
    await runner.check_pool()
    assert reminder.await_count == 2


async def test_reminder_limit_survives_a_restart(session_maker) -> None:
    first = AsyncMock()
    await _external_runner(session_maker, reminder=first).check_pool()
    second = AsyncMock()
    await _external_runner(session_maker, reminder=second).check_pool()
    first.assert_awaited_once()
    second.assert_not_awaited()


async def test_enough_leads_means_no_reminder(session_maker) -> None:
    async with session_maker() as session:
        session.add(Lead(external_id="a", source="x", name="n", category="c", site_status="no_site", crm_status="new"))
        await session.commit()
    reminder = AsyncMock()
    await _external_runner(session_maker, reminder=reminder, min_pool=1).check_pool()
    reminder.assert_not_awaited()


async def test_failing_reminder_does_not_raise(session_maker) -> None:
    await _external_runner(session_maker, reminder=AsyncMock(side_effect=RuntimeError("telegram"))).check_pool()


async def test_short_batch_in_external_mode_asks_for_a_file(session_maker) -> None:
    reminder = AsyncMock()
    runner = _external_runner(session_maker, reminder=reminder)
    decision = await runner.request_topup("batch")
    assert decision.outcome is TopupOutcome.EXTERNAL
    assert not runner.is_running
    reminder.assert_awaited_once()
    assert "нужен новый файл" in issue._describe_topup(TopupDecision(TopupOutcome.EXTERNAL))


async def test_normal_mode_still_collects_as_before(session_maker) -> None:
    provider = Provider()
    runner = CollectorRunner(session_maker, ProviderSet(providers=[provider]), _config(), AsyncMock(), min_pool=60)
    reports = asyncio.Event()
    runner._report = AsyncMock(side_effect=lambda report: reports.set())
    await runner.check_pool()
    await asyncio.wait_for(reports.wait(), 5)
    assert provider.calls == 1


@pytest.mark.parametrize(
    "handler",
    [
        collector_handlers.handle_collect_now,
        collector_handlers.handle_collect_on,
        collector_handlers.handle_collect_off,
    ],
)
async def test_commands_say_that_collection_is_external(handler) -> None:
    runner = MagicMock()
    runner.external_mode = True
    runner.set_auto_enabled = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock()

    await handler(message, runner)

    assert "внешнем сборщике" in message.answer.await_args.args[0]
    runner.start_in_background.assert_not_called()
    runner.set_auto_enabled.assert_not_awaited()


async def test_status_in_external_mode_shows_imports_and_the_pool(session_maker) -> None:
    runner = _external_runner(session_maker)
    status = await runner.get_status()
    text = format_collector_status(status)
    assert "внешний сборщик" in text
    assert "Файлы ещё не импортировались" in text

    async with session_maker() as session:
        session.add(
            ImportLog(source="inbox", file_name="<x>.json", ok=True, total=5, saved_new=3, duplicates=1, rejected=1)
        )
        session.add(
            Lead(
                external_id="a", source="x", name="n", category="c", site_status="no_site", crm_status="new",
                imported_at=datetime.now(UTC),
            )
        )
        await session.commit()

    text = format_collector_status(await runner.get_status())
    assert "&lt;x&gt;.json" in text
    assert "новых: <b>3</b>" in text
    assert "всего пришло лидов <b>1</b>" in text
    assert "В запасе: <b>1</b>" in text


async def test_status_in_normal_mode_also_shows_imports(session_maker) -> None:
    runner = CollectorRunner(session_maker, ProviderSet(providers=[Provider()]), _config(), AsyncMock())
    text = format_collector_status(await runner.get_status())
    assert "Автозапуск" in text
    assert "Импорт файлов" in text


async def test_failed_import_is_shown_in_the_status(session_maker) -> None:
    async with session_maker() as session:
        session.add(ImportLog(source="inbox", file_name="a.json", ok=False, error="<битый файл>"))
        await session.commit()
    text = format_collector_status(await _external_runner(session_maker).get_status())
    assert "Не выполнен" in text and "&lt;битый файл&gt;" in text


async def test_import_result_goes_to_chats_and_admins_except_the_one_it_came_from() -> None:
    from src.services.lead_import import SOURCE_TELEGRAM, ImportResult

    bot = MagicMock()
    bot.send_message = AsyncMock()
    notifier = AdminNotifier(bot, [100], [-1001, -1002])
    await notifier.report_import(ImportResult(source=SOURCE_TELEGRAM, file_name="a.json"), exclude_chat=-1001)
    assert sorted(call.args[0] for call in bot.send_message.await_args_list) == [-1002, 100]


async def test_pool_reminder_text() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await AdminNotifier(bot, [100], [-1001]).remind_pool_low(7)
    assert bot.send_message.await_count == 2
    text = bot.send_message.await_args.args[1]
    assert "<b>7</b>" in text and "нужен новый импорт" in text


async def test_inbox_job_runs_through_the_scheduler(session_maker) -> None:
    runner = _external_runner(session_maker)
    job = AsyncMock()
    runner.add_interval_job(job, 300, "import_inbox")
    await runner.start()
    try:
        assert runner._scheduler.get_job("import_inbox").max_instances == 1
    finally:
        await runner.shutdown()


async def test_upgrade_adds_the_reminder_column_to_an_existing_state_table(tmp_path: Path) -> None:
    from sqlalchemy import text

    from src.database.repo import init_db

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE collector_state (id INTEGER PRIMARY KEY, cursor INTEGER)"))
        await conn.execute(
            text(
                "CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, external_id VARCHAR(128) NOT NULL, "
                "source VARCHAR(32) NOT NULL, name VARCHAR(255) NOT NULL, category VARCHAR(128) NOT NULL, "
                "district VARCHAR(128), address VARCHAR(512), phone VARCHAR(128), socials VARCHAR(512), "
                "raw_website VARCHAR(512), site_status VARCHAR(32) NOT NULL, score INTEGER NOT NULL, "
                "crm_status VARCHAR(32) NOT NULL, map_url VARCHAR(512), created_at DATETIME)"
            )
        )
    await init_db(engine)
    await init_db(engine)
    async with engine.connect() as conn:
        state_columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(collector_state)"))}
        lead_columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(leads)"))}
    await engine.dispose()
    assert "last_pool_reminder_at" in state_columns
    assert {"exported_at", "imported_at"} <= lead_columns
    assert CollectorState.__tablename__ == "collector_state"


def test_export_is_valid_json_on_disk(tmp_path: Path) -> None:
    from src.services.lead_export import _write_file

    path = _write_file(tmp_path, datetime(2026, 11, 5, 1, 2, 3), {"кириллица": "да"})
    assert path.name == "leads_20261105_010203.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"кириллица": "да"}
    assert "кириллица" in path.read_text(encoding="utf-8")  # readable, not \\u escapes
