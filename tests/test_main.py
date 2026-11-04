"""Tests for application entrypoint configuration checks."""

import pytest

import main
from src.config import Settings


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
