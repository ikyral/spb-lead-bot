"""Files between the bot and a collector, in both directions. Access is restricted by AdminOnlyMiddleware."""

import html
import io
import json
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, LinkPreviewOptions, Message

from src.bot.formatters import format_import_messages, format_niche_table, format_unknown_niche
from src.database.repo import async_session_maker
from src.services.lead_file import (
    ENRICH_REQUEST_MAX_LEADS,
    KIND_ENRICH_REQUEST,
    build_enrich_document,
    enrich_request_record,
)
from src.services.lead_import import SOURCE_TELEGRAM, ImportService
from src.services.niche_priority import (
    MAX_LEADS_PER_NICHE,
    choose_export,
    known_niches,
    load_niche_stats,
    resolve_niche,
)
from src.services.providers.base import niche_title

logger = logging.getLogger(__name__)
router = Router(name="imports")

EXPORT_UNKNOWN_DEFAULT: int = 30
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _say(message: Message, status: Message | None, text: str) -> None:
    """Replace the 'importing' note, or write anew if that is impossible. Failing to say it is not fatal."""
    try:
        if status is not None:
            await status.edit_text(text, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
        else:
            await message.answer(text, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
    except TelegramAPIError as exc:
        logger.warning("Could not send the import result: %s", exc)


def _parse_export_args(args: str | None) -> tuple[int | None, str | None]:
    """The number of leads and the niche, in the order /export_unknown [N] [ниша]. The number 0 for a wrong one."""
    first, _, rest = (args or "").strip().partition(" ")
    if first.isascii() and first.isdecimal():
        return int(first), rest.strip() or None
    return None, (args or "").strip() or None


async def _answer_all(message: Message, texts: list[str]) -> None:
    try:
        for text in texts:
            await message.answer(text, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
    except TelegramAPIError as exc:
        logger.warning("Could not send a message about niches: %s", exc)


def _export_caption(count: int, limit: int, explored: int, niche: str | None) -> str:
    parts = [f"Лидов в файле: {count}"]
    if niche is not None:
        parts.append(f"ниша: {niche_title(niche)}")
    elif explored:
        parts.append(f"из них для проверки новых ниш: {explored}")
    text = ", ".join(parts) + "."
    if niche is None and count < limit:
        text += f" Меньше {limit}: из одной ниши берётся не больше {MAX_LEADS_PER_NICHE}."
    return (
        f"{text} На компьютере со сборщиком: "
        "collect_cli.py --assisted --enrich этот_файл, а файл с результатом отправьте сюда."
    )


@router.message(Command("export_unknown"))
async def handle_export_unknown(message: Message, command: CommandObject) -> None:
    """Send a file with leads nobody has looked at, from the most promising niches, for a collector to read."""
    number, niche_text = _parse_export_args(command.args)
    if number == 0:
        await message.answer(f"Использование: /export_unknown [N] [ниша], N от 1 до {ENRICH_REQUEST_MAX_LEADS}.")
        return
    limit = min(number, ENRICH_REQUEST_MAX_LEADS) if number else EXPORT_UNKNOWN_DEFAULT

    niche: str | None = None
    async with async_session_maker() as session:
        if niche_text is not None:
            niches = await known_niches(session)
            niche = resolve_niche(niche_text, niches)
            if niche is None:
                await _answer_all(message, format_unknown_niche(niche_text, niches))
                return
        plan = await choose_export(session, limit, niche)
    if not plan.leads:
        where = f" в нише {html.escape(niche_title(niche))}" if niche is not None else ""
        await message.answer(f"Лидов с неизвестным статусом сайта и ссылкой на карточку 2ГИС{where} в запасе нет.")
        return

    now = datetime.now(UTC)
    records = [enrich_request_record(lead) for lead in plan.leads]
    document = build_enrich_document(KIND_ENRICH_REQUEST, records, now)
    content = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        await message.answer_document(
            BufferedInputFile(content, filename=f"enrich_request_{now.strftime('%Y%m%d_%H%M%S')}.json"),
            caption=html.escape(_export_caption(len(plan.leads), limit, plan.explored, niche)),
        )
    except TelegramAPIError as exc:
        logger.warning("Could not send the file for a collector: %s", exc)


@router.message(Command("niches"))
async def handle_niches(message: Message) -> None:
    """Show what is known about sites in each niche, which is what the order of /export_unknown comes from."""
    async with async_session_maker() as session:
        stats = await load_niche_stats(session)
    await _answer_all(message, format_niche_table(stats))


@router.message(F.document, F.document.file_name.lower().endswith(".json"))
async def handle_lead_file(message: Message, bot: Bot, import_service: ImportService) -> None:
    """Download a lead file and import it."""
    document = message.document
    if document is None:
        return
    if document.file_size is not None and document.file_size > import_service.max_bytes:
        await _say(message, None, f"⚠️ Файл больше допустимых {import_service.max_bytes // 1024} КБ, он не загружен.")
        return

    status: Message | None = None
    try:
        status = await message.answer("⏳ Импортирую файл, проверяю сайты новых организаций...")
        buffer = io.BytesIO()
        await bot.download(document, destination=buffer)
    except TelegramAPIError as exc:
        logger.warning("Could not download the lead file: %s", exc)
        await _say(message, status, "⚠️ Не удалось скачать файл из Telegram. Отправьте его ещё раз.")
        return

    # This chat gets the result as an edit of the note, the others as a notification.
    result = await import_service.import_bytes(
        buffer.getvalue(), document.file_name, SOURCE_TELEGRAM, exclude_chat=message.chat.id
    )
    first, *others = format_import_messages(result)
    await _say(message, status, first)
    for text in others:
        await _say(message, None, text)
