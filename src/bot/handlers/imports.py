"""Files between the bot and a collector, in both directions. Access is restricted by AdminOnlyMiddleware."""

import io
import json
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from src.bot.formatters import format_import_report
from src.database.repo import async_session_maker, get_unknown_site_leads
from src.services.lead_file import (
    ENRICH_REQUEST_MAX_LEADS,
    KIND_ENRICH_REQUEST,
    build_enrich_document,
    enrich_request_record,
)
from src.services.lead_import import SOURCE_TELEGRAM, ImportService

logger = logging.getLogger(__name__)
router = Router(name="imports")

EXPORT_UNKNOWN_DEFAULT: int = 30


async def _say(message: Message, status: Message | None, text: str) -> None:
    """Replace the 'importing' note, or write anew if that is impossible. Failing to say it is not fatal."""
    try:
        if status is not None:
            await status.edit_text(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
    except TelegramAPIError as exc:
        logger.warning("Could not send the import result: %s", exc)


@router.message(Command("export_unknown"))
async def handle_export_unknown(message: Message, command: CommandObject) -> None:
    """Send a file with the best leads nobody has looked at, for a collector to read their cards."""
    argument = (command.args or "").strip()
    if argument and not (argument.isascii() and argument.isdecimal() and int(argument) > 0):
        await message.answer(f"Использование: /export_unknown [N], N от 1 до {ENRICH_REQUEST_MAX_LEADS}.")
        return
    limit = min(int(argument), ENRICH_REQUEST_MAX_LEADS) if argument else EXPORT_UNKNOWN_DEFAULT

    async with async_session_maker() as session:
        leads = await get_unknown_site_leads(session, limit)
    if not leads:
        await message.answer("Лидов с неизвестным статусом сайта и ссылкой на карточку 2ГИС в запасе нет.")
        return

    now = datetime.now(UTC)
    document = build_enrich_document(KIND_ENRICH_REQUEST, [enrich_request_record(lead) for lead in leads], now)
    content = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        await message.answer_document(
            BufferedInputFile(content, filename=f"enrich_request_{now.strftime('%Y%m%d_%H%M%S')}.json"),
            caption=(
                f"Лидов в файле: {len(leads)}. На компьютере со сборщиком: "
                "collect_cli.py --assisted --enrich этот_файл, а файл с результатом отправьте сюда."
            ),
        )
    except TelegramAPIError as exc:
        logger.warning("Could not send the file for a collector: %s", exc)


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
    await _say(message, status, format_import_report(result))
