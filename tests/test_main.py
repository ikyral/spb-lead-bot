"""Tests for application entrypoint configuration checks."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

import main
from src.bot.digest import DIGEST_JOB_ID, DailyDigest
from src.config import Settings


def test_httpx_request_urls_are_not_logged_at_info() -> None:
    """httpx logs full request URLs at INFO, and 2GIS URLs carry the API key in '?key=...'."""
    # A fresh process: under pytest the root logger already has handlers and basicConfig is a no-op.
    code = "import logging, main; print(logging.getLogger('httpx').getEffectiveLevel())"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert int(result.stdout.strip().splitlines()[-1]) >= logging.WARNING


@pytest.mark.asyncio
async def test_main_refuses_to_start_with_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken .env must stop startup with the real reason, before touching the database."""

    async def fail_init_db() -> None:
        raise AssertionError("init_db must not run with invalid configuration")

    monkeypatch.setattr(main, "init_db", fail_init_db)
    monkeypatch.setattr(main, "settings_error", ValueError("ADMIN_TG_ID: Input should be a valid integer"))

    with pytest.raises(SystemExit, match="ADMIN_TG_ID: Input should be a valid integer"):
        await main.main()


@pytest.mark.asyncio
async def test_main_refuses_to_start_without_token_or_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty BOT_TOKEN or ADMIN_TG_ID=0 (fallback values) must not start the bot."""

    async def fail_init_db() -> None:
        raise AssertionError("init_db must not run with invalid configuration")

    monkeypatch.setattr(main, "init_db", fail_init_db)
    monkeypatch.setattr(main, "settings_error", None)
    monkeypatch.setattr(main, "settings", Settings(BOT_TOKEN="", ADMIN_TG_ID=0))

    with pytest.raises(SystemExit, match="BOT_TOKEN and ADMIN_TG_ID must be set"):
        await main.main()


@pytest.mark.asyncio
async def test_main_refuses_to_start_with_invalid_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong schedule must stop startup with a message that names the variable."""

    async def fail_init_db() -> None:
        raise AssertionError("init_db must not run with invalid configuration")

    monkeypatch.setattr(main, "init_db", fail_init_db)
    monkeypatch.setattr(main, "settings_error", None)
    monkeypatch.setattr(main, "settings", Settings(BOT_TOKEN="t", ADMIN_TG_ID=1, COLLECT_CRON="не расписание"))

    with pytest.raises(SystemExit, match="COLLECT_CRON"):
        await main.main()


@pytest.mark.asyncio
async def test_main_wires_the_collector_into_the_bot_lifetime(monkeypatch: pytest.MonkeyPatch, session_maker) -> None:
    """By default only OSM is used; the scheduler runs exactly while polling runs."""
    from aiogram import Dispatcher

    seen: dict = {}

    async def fake_polling(self: Dispatcher, bot, **kwargs) -> None:
        await self.emit_startup(bot=bot, **self.workflow_data)
        runner = self["collector_runner"]
        seen["status"] = await runner.get_status()
        seen["routers"] = {router.name for router in self.sub_routers}
        seen["digest_job"] = runner._scheduler.get_job(DIGEST_JOB_ID)
        seen["scheduler_running"] = runner._scheduler.running
        await self.emit_shutdown(bot=bot, **self.workflow_data)
        seen["scheduler_running_after"] = runner._scheduler.running
        await bot.session.close()

    async def no_init_db() -> None:
        return None

    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    monkeypatch.setattr(main, "init_db", no_init_db)
    monkeypatch.setattr(main, "async_session_maker", session_maker)
    monkeypatch.setattr(main, "settings_error", None)
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, BOT_TOKEN="123456:TEST", ADMIN_TG_ID=1))
    # The command menu fails, as it does when an admin has never pressed /start: startup must go on.
    set_commands = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="chat not found"))
    monkeypatch.setattr(Bot, "set_my_commands", set_commands)
    # The plan runs once right after the start: here it must not reach Telegram.
    monkeypatch.setattr(DailyDigest, "send_scheduled", AsyncMock())

    await main.main()

    status = seen["status"]
    assert set_commands.await_count == 2  # everyone, then the admin
    assert seen["routers"] == {"search", "leads", "issue", "collector", "imports", "reminders", "menu", "fallback"}
    assert seen["digest_job"] is not None  # DIGEST_ENABLED is on by default
    assert seen["scheduler_running"] is True
    assert seen["scheduler_running_after"] is False
    assert status.next_run_at is not None
    assert status.active_providers == ["osm"]
    assert status.disabled_providers == {"2gis": "не задан DGIS_API_KEY"}  # still runs, on OSM
    assert status.dgis_key is None
    assert status.batch_size == 30


@pytest.mark.asyncio
async def test_main_counts_requests_of_the_key_and_warns_about_its_end(
    monkeypatch: pytest.MonkeyPatch, session_maker
) -> None:
    """With a key the limits must really reach the provider: a test of each part would not notice a missing link."""
    from aiogram import Dispatcher

    from src.bot.notifier import AdminNotifier
    from src.services.scheduler import KEY_CHECK_JOB_ID

    seen: dict = {}

    async def fake_polling(self: Dispatcher, bot, **kwargs) -> None:
        await self.emit_startup(bot=bot, **self.workflow_data)
        runner = self["collector_runner"]
        seen["status"] = await runner.get_status()
        seen["provider"] = runner._provider_set.providers[0]
        seen["job"] = runner._scheduler.get_job(KEY_CHECK_JOB_ID)
        seen["notice"] = runner._key_notice
        await self.emit_shutdown(bot=bot, **self.workflow_data)
        await bot.session.close()

    async def no_init_db() -> None:
        return None

    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    monkeypatch.setattr(main, "init_db", no_init_db)
    monkeypatch.setattr(main, "async_session_maker", session_maker)
    monkeypatch.setattr(main, "settings_error", None)
    current = Settings(
        _env_file=None,
        BOT_TOKEN="123456:TEST",
        ADMIN_TG_ID=1,
        DGIS_API_KEY="KEY",
        DGIS_KEY_CREATED_AT="2026-11-01",
        DGIS_REQUEST_CONTACTS=True,
    )
    monkeypatch.setattr(main, "settings", current)

    # Routers are module globals, and main() has already run in this process.
    routers = (
        main.search_router,
        main.leads_router,
        main.issue_router,
        main.collector_router,
        main.imports_router,
        main.reminders_router,
        main.menu_router,
        main.fallback_router,
    )
    for router in routers:
        router._parent_router = None
    monkeypatch.setattr(Bot, "set_my_commands", AsyncMock())
    monkeypatch.setattr(DailyDigest, "send_scheduled", AsyncMock())

    await main.main()

    status = seen["status"]
    assert status.active_providers == ["2gis", "osm"]
    assert (status.dgis_key.used, status.dgis_key.limit, status.dgis_key.reserve) == (0, 1000, 50)
    assert status.dgis_key.expires_on.isoformat() == "2026-12-01"
    assert status.dgis_key.request_contacts is True

    provider = seen["provider"]
    assert provider._options.request_contacts is True
    assert provider._api._before_request is not None
    await provider._api._before_request()
    async with session_maker() as session:
        from src.database.repo import get_provider_states

        assert (await get_provider_states(session))["2gis"].requests_total == 1

    assert seen["job"] is not None
    assert seen["notice"].__self__.__class__ is AdminNotifier


def test_invalid_value_in_environment_is_reported_not_a_crash_on_import() -> None:
    """Settings() is created on import: a bad value must reach main() as settings_error, not as a traceback."""
    code = "from src.config import settings_error as e; print(type(e).__name__, 'COLLECT_MAX_PAGES' in str(e))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={**os.environ, "COLLECT_MAX_PAGES": "много"},
    )
    assert result.stdout.strip().splitlines()[-1] == "ValidationError True"


def test_secrets_are_not_exposed_in_settings_repr() -> None:
    """Settings may end up in logs or tracebacks; tokens must be masked there."""
    s = Settings(BOT_TOKEN="123456:SECRET-BOT-TOKEN", ADMIN_TG_ID=1, TWOGIS_API_KEY="SECRET-2GIS-KEY")
    for text in (repr(s), str(s), repr(s.model_dump())):
        assert "SECRET-BOT-TOKEN" not in text
        assert "SECRET-2GIS-KEY" not in text
    assert s.BOT_TOKEN.get_secret_value() == "123456:SECRET-BOT-TOKEN"


def test_importing_providers_does_not_create_the_database_engine() -> None:
    """Formatters/keyboards import providers.base; that must not pull in the pipeline and the DB engine."""
    code = (
        "import sys, src.services.providers.base, src.bot.formatters; "
        "print('src.database.repo' in sys.modules, 'src.services.pipeline' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "False False"


def test_services_package_still_exports_pipeline_functions() -> None:
    from src.services import import_csv_harvest, run_lead_harvest
    from src.services.pipeline import import_csv_harvest as direct

    assert import_csv_harvest is direct and callable(run_lead_harvest)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("db_url", "expected"),
    [
        ("sqlite+aiosqlite:///./leads.db", f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'leads.db').as_posix()}"),
        ("sqlite+aiosqlite:///data/my.db", f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'my.db').as_posix()}"),
        ("sqlite+aiosqlite:////app/db/leads.db", "sqlite+aiosqlite:////app/db/leads.db"),
        ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        ("postgresql+asyncpg://user:pass@db/leads", "postgresql+asyncpg://user:pass@db/leads"),
    ],
)
def test_relative_sqlite_path_is_resolved_against_project_root(db_url: str, expected: str) -> None:
    """'./leads.db' used to depend on the current directory: started elsewhere, the bot got an empty DB."""
    assert expected == Settings(BOT_TOKEN="t", ADMIN_TG_ID=1, DB_URL=db_url).DB_URL
