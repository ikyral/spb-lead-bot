from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy models."""

    pass


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
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Lead id={self.id} external_id={self.external_id!r} "
            f"name={self.name!r} score={self.score} crm_status={self.crm_status!r}>"
        )
