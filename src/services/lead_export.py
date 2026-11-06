"""Writes leads a collector has not handed over yet to files in the exports folder."""

import asyncio
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repo import get_unexported_leads, mark_leads_exported
from src.services.lead_file import KIND_ENRICH_RESULT, build_document, build_enrich_document, lead_to_record


@dataclass
class ExportResult:
    """How many leads went out and where."""

    count: int = 0
    files: list[Path] = field(default_factory=list)


def _unique_path(export_dir: Path, now: datetime, prefix: str) -> Path:
    """leads_YYYYMMDD_HHMMSS.json; several files within a second get a number."""
    stem = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}"
    path = export_dir / f"{stem}.json"
    number = 1
    while path.exists():
        number += 1
        path = export_dir / f"{stem}_{number}.json"
    return path


def _write_file(export_dir: Path, now: datetime, document: dict[str, Any], prefix: str = "leads") -> Path:
    """Write through a temporary file: nobody can pick up a half-written export."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(export_dir, now, prefix)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return path


def write_enrich_result(export_dir: Path, records: list[dict[str, Any]]) -> Path:
    """Write a result of reading cards. Not async on purpose: it also runs while the run is being cancelled."""
    now = datetime.now().astimezone()
    return _write_file(export_dir, now, build_enrich_document(KIND_ENRICH_RESULT, records, now), prefix="enrich")


async def export_new_leads(
    session_maker: async_sessionmaker[AsyncSession],
    export_dir: Path,
    city_slug: str,
    max_leads_per_file: int,
) -> ExportResult:
    """
    Write all leads not exported yet, each file within the server's limit, and mark them.

    The mark comes after the file is written. If the process dies in between, the leads are
    exported again next time, which is safe: importing is idempotent.
    """
    result = ExportResult()
    host = socket.gethostname() or None
    while True:
        async with session_maker() as session:
            leads = await get_unexported_leads(session, max_leads_per_file)
            if not leads:
                return result
            now = datetime.now().astimezone()
            records = [lead_to_record(lead, city_slug) for lead in leads]
            document = build_document(records, now, host)
            path = await asyncio.to_thread(_write_file, export_dir, now, document)
            await mark_leads_exported(session, [lead.id for lead in leads], datetime.now(UTC))
        result.count += len(records)
        result.files.append(path)
