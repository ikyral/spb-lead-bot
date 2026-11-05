"""Shared test setup."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import Base
from src.services.api_client import RateLimiter
from src.services.providers import osm, twogis


@pytest.fixture(autouse=True)
def _no_rate_limit_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Providers share real rate limiters; tests talk to mock transports and must not wait."""
    monkeypatch.setattr(twogis, "_RATE_LIMITER", RateLimiter(0))
    monkeypatch.setattr(osm, "_RATE_LIMITER", RateLimiter(0))


@pytest.fixture
async def session_maker(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A database in a file: unlike a shared in-memory connection, concurrent sessions are really separate."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()
