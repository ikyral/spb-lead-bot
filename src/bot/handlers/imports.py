"""Importing leads from a file sent to the bot. Access is restricted by AdminOnlyMiddleware."""

import io
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from src.bot.formatters import format_import_report
from src.services.lead_import import SOURCE_TELEGRAM, ImportService

logger = logging.getLogger(__name__)
router = Router(name="imports")


async def _say(message: Message, status: Message | None, text: str) -> None:
    """Replace the 'importing' note, or write anew if that is impossible. Failing to say it is not fatal."""
    try:
        if status is not None:
            await status.edit_text(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
    except TelegramAPIError as exc:
        logger.warning("Could not send the import result: %s", exc)


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
