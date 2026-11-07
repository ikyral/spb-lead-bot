"""Brings leads from export files into the database, from Telegram and from the inbox folder."""

import asyncio
import logging
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repo import add_import_log, count_new_leads
from src.services.lead_enrich import EnrichStats, apply_enrichment
from src.services.lead_file import (
    KIND_ENRICH_REQUEST,
    KIND_ENRICH_RESULT,
    REQUEST_FILE_ERROR,
    LeadFileError,
    clean_text,
    file_kind,
    parse_enrich_result,
    parse_lead_file,
)
from src.services.pipeline import save_collected_places

logger = logging.getLogger(__name__)

SOURCE_TELEGRAM = "telegram"
SOURCE_INBOX = "inbox"
REASON_NOT_QUALIFIED = "не прошли проверку сайта и оценку (есть рабочий сайт или мало данных)"

# A file changed this recently may still be uploading (scp writes under the final name).
INBOX_MIN_AGE_SECONDS: float = 30.0
_MAX_FILES_PER_SCAN: int = 20
_FILE_NAME_MAX = 255


@dataclass
class ImportResult:
    """Outcome of importing one file. Only counts and fixed reasons, except for `enrichment.useful` in the report."""

    source: str
    file_name: str
    ok: bool = True
    error: str | None = None
    total: int = 0
    saved_new: int = 0
    duplicates: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    pool_size: int = 0
    # Set for a file with results of reading cards: it updates known leads instead of adding new ones.
    enrichment: EnrichStats | None = None

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


ReportCallback = Callable[[ImportResult, int | None], Awaitable[None]]


def _list_ready_files(inbox_dir: Path, min_age_seconds: float) -> list[Path]:
    """Files that can be taken now. Links are skipped: nothing outside the folder gets read or moved."""
    # Created in advance: scp into a folder that does not exist would fail.
    inbox_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    ready = [
        path
        for path in inbox_dir.iterdir()
        if path.suffix.lower() == ".json"
        and path.is_file()
        and not path.is_symlink()
        and now - path.stat().st_mtime >= min_age_seconds
    ]
    return sorted(ready)[:_MAX_FILES_PER_SCAN]


def read_limited(path: Path, max_bytes: int) -> bytes:
    """One byte over the limit is enough for the size check to refuse the file."""
    with path.open("rb") as file:
        return file.read(max_bytes + 1)


def _move_to(path: Path, target_dir: Path, reason: str | None = None) -> Path:
    """Move without overwriting an earlier file of the same name."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}{path.suffix}"
    os.replace(path, target)
    if reason is not None:
        target.with_name(f"{target.name}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    return target


class ImportService:
    """One import at a time, wherever it came from: parallel ones could not see each other's leads."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        report: ReportCallback,
        inbox_dir: Path,
        max_bytes: int,
        max_leads: int,
        require_contact: bool = False,
    ) -> None:
        self._session_maker = session_maker
        self._report = report
        self._inbox_dir = inbox_dir
        self.max_bytes = max_bytes
        self._max_leads = max_leads
        self._require_contact = require_contact
        self._lock = asyncio.Lock()

    async def import_bytes(
        self, data: bytes, file_name: str | None, source: str, exclude_chat: int | None = None
    ) -> ImportResult:
        """Import a file. Never raises because of the file; the outcome is reported and returned."""
        name = clean_text(file_name or "")[:_FILE_NAME_MAX] or "без имени"
        async with self._lock:
            result = await self._import(data, name, source)
        logger.info(
            "Import of %s from %s: ok=%s total=%d new=%d duplicates=%d rejected=%d",
            name,
            source,
            result.ok,
            result.total,
            result.saved_new,
            result.duplicates,
            result.rejected_total,
        )
        try:
            await self._report(result, exclude_chat)
        except Exception:
            logger.exception("Failed to report the import result")
        return result

    async def _import(self, data: bytes, name: str, source: str) -> ImportResult:
        result = ImportResult(source=source, file_name=name)
        try:
            kind = await asyncio.to_thread(file_kind, data, self.max_bytes)
            if kind == KIND_ENRICH_RESULT:
                await self._import_enrichment(data, result)
            elif kind == KIND_ENRICH_REQUEST:
                raise LeadFileError(REQUEST_FILE_ERROR)
            elif kind is not None:
                raise LeadFileError("неизвестный тип файла в поле kind")
            else:
                await self._import_leads(data, result)
        except LeadFileError as exc:
            result.ok, result.error = False, str(exc)
        except Exception:
            logger.exception("Import of %s failed", name)
            result.ok, result.error = False, "внутренняя ошибка импорта, подробности в логе бота"

        try:
            async with self._session_maker() as session:
                result.pool_size = await count_new_leads(session)
                await add_import_log(
                    session,
                    source=source,
                    file_name=name,
                    ok=result.ok,
                    error=result.error,
                    total=result.total,
                    saved_new=result.saved_new,
                    duplicates=result.duplicates,
                    rejected=result.rejected_total,
                )
        except Exception:
            logger.exception("Could not record the import of %s", name)
        return result

    async def _import_leads(self, data: bytes, result: ImportResult) -> None:
        parsed = await asyncio.to_thread(parse_lead_file, data, self.max_bytes, self._max_leads, self._require_contact)
        result.total = parsed.total
        result.rejected.update(parsed.rejected)
        if parsed.places:
            async with self._session_maker() as session:
                stats = await save_collected_places(
                    session, parsed.places, extra_fields={"imported_at": datetime.now(UTC)}
                )
            result.saved_new = stats["saved_new"]
            result.duplicates = stats["duplicates_skipped"]
            not_qualified = stats["total_found"] - stats["saved_new"] - stats["duplicates_skipped"]
            if not_qualified:
                result.rejected[REASON_NOT_QUALIFIED] += not_qualified

    async def _import_enrichment(self, data: bytes, result: ImportResult) -> None:
        parsed = await asyncio.to_thread(parse_enrich_result, data, self.max_bytes, self._max_leads)
        result.total = parsed.total
        result.enrichment = await apply_enrichment(self._session_maker, parsed)
        # In the log of imports these are the records that did not change anything.
        result.rejected.update(result.enrichment.ignored)

    async def scan_inbox(self) -> int:
        """Import everything ready in the folder. Returns the number of files handled. Never raises."""
        handled = 0
        try:
            paths = await asyncio.to_thread(_list_ready_files, self._inbox_dir, INBOX_MIN_AGE_SECONDS)
        except OSError as exc:
            logger.warning("Cannot read the inbox folder %s: %s", self._inbox_dir, exc)
            return handled
        for path in paths:
            try:
                await self._handle_inbox_file(path)
                handled += 1
            except Exception:
                # The file stays where it is and is tried again; the next ones must not suffer.
                logger.exception("Could not process inbox file %s", path.name)
        return handled

    async def _handle_inbox_file(self, path: Path) -> None:
        data = await asyncio.to_thread(read_limited, path, self.max_bytes)
        result = await self.import_bytes(data, path.name, SOURCE_INBOX)
        if result.ok:
            await asyncio.to_thread(_move_to, path, self._inbox_dir / "done")
        else:
            logger.warning("Inbox file %s moved to failed: %s", path.name, result.error)
            await asyncio.to_thread(_move_to, path, self._inbox_dir / "failed", result.error)
