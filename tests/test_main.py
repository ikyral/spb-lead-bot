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
