from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Dialect, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Lead lifecycle (leads.crm_status). A lead leaves STATUS_NEW only once, so it is handed out only once.
STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_REJECTED = "rejected"
STATUS_ISSUED = "issued"

# What came of contacting a handed-out lead (leads.contact_status).
CONTACT_CONTACTED = "contacted"
CONTACT_REPLIED = "replied"
CONTACT_REJECTED = "rejected"
CONTACT_NO_ANSWER = "no_answer"


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
        # Handing out batches: WHERE crm_status = 'new' ORDER BY created_at, id.
        Index("ix_leads_crm_status_created_at", "crm_status", "created_at"),
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
    # Added after the first release: existing databases get them from NEW_LEAD_COLUMNS in repo.py.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    contact_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # Who set contact_status and when: Telegram IDs do not fit 32 bits.
    contact_status_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    contact_status_by: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    contact_status_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # On a collector's computer: when the lead went into an export file.
    exported_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # On the server: when the lead arrived from an export file.
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # The source's own category. `category` above is the niche the organization was searched for.
    rubric: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

    @property
    def has_contacts(self) -> bool:
        """Is there anything to contact the organization by, without opening its card?"""
        return any(value and value.strip() for value in (self.phone, self.raw_website, self.socials, self.email))

    def __repr__(self) -> str:
        return (
            f"<Lead id={self.id} external_id={self.external_id!r} "
            f"name={self.name!r} score={self.score} crm_status={self.crm_status!r}>"
        )


class ProviderState(Base):
    """How a source has been treating the bot: blocks, the pause after them, automatic switch-off."""

    __tablename__ = "provider_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_blocks: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # Set after too many blocks in a row; only an admin's command clears it.
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    last_block_reason: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    last_block_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # API requests spent with the key identified by key_fingerprint (a hash, never the key itself).
    requests_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    key_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)


class ImportLog(Base):
    """One imported file: only counts and fixed reasons, never the content of leads."""

    __tablename__ = "import_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(UTC))
    source: Mapped[str] = mapped_column(String(32))
    file_name: Mapped[str] = mapped_column(String(255))
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    total: Mapped[int] = mapped_column(Integer, default=0)
    saved_new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)


class CollectorState(Base):
    """The only row (id=1) holds the automatic collection switch and the result of the last run."""

    __tablename__ = "collector_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # None = not switched by a command yet, COLLECT_ENABLED applies.
    auto_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    last_trigger: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    last_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    found: Mapped[int] = mapped_column(Integer, default=0)
    qualified: Mapped[int] = mapped_column(Integer, default=0)
    saved_new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)
    # The last alert sent to admins: the same problem is reported once, not after every run.
    last_alert: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)
    # Position in the list of search targets, so a limited run continues where the previous one stopped.
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    # API requests spent on requests_day (date in COLLECT_TIMEZONE, ISO format).
    requests_day: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
    # With an external collector: when admins were last asked to bring a new file.
    last_pool_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    # The last warning about the API key running out: '<expiry date>:<day of the warning or "expired">'.
    key_warning_mark: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
