"""Tests for application entrypoint configuration checks."""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

import main
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
        capture_output=True, text=True, timeout=60, check=True,
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
    assert Settings(BOT_TOKEN="t", ADMIN_TG_ID=1, DB_URL=db_url).DB_URL == expected
