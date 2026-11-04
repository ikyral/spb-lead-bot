"""Entrypoint for SPb Lead Finder Telegram Bot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.bot.handlers.leads import router as leads_router
from src.bot.handlers.search import router as search_router
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import settings
from src.database.repo import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Initialize database and start bot polling."""
    await init_db()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    admin_guard = AdminOnlyMiddleware()
    dp.message.middleware(admin_guard)
    dp.callback_query.middleware(admin_guard)

    dp.include_router(search_router)
    dp.include_router(leads_router)

    logger.info("Bot starting polling for admin ID %s...", settings.ADMIN_TG_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
