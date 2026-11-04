from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """
    Timezone-aware UTC datetime that also works on SQLite.

    SQLite has no time zones: tzinfo is dropped on write (without converting) and values
    come back naive. Here aware values are converted to UTC before storing, and values
    read back are marked as UTC. The column DDL stays DATETIME, so no migration is needed.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy models."""


class Lead(Base):
    """Lead model representing a local business entity."""

    __tablename__ = "leads"
    __table_args__ = (
        # Lead cards: WHERE crm_status = ? AND score >= ? ORDER BY score DESC, id DESC.
        # SQLite stores the rowid (id) in the index, so the ORDER BY needs no extra sort.
        Index("ix_leads_crm_status_score", "crm_status", "score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(128))
    district: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    phone: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    socials: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    raw_website: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    site_status: Mapped[str] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer, default=0)
    crm_status: Mapped[str] = mapped_column(String(32), default="new")
    map_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Lead id={self.id} external_id={self.external_id!r} "
            f"name={self.name!r} score={self.score} crm_status={self.crm_status!r}>"
        )
