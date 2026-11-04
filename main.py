"""Entrypoint for SPb Lead Finder Telegram Bot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.bot.handlers.leads import router as leads_router
from src.bot.handlers.search import router as search_router
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import settings, settings_error
from src.database.repo import engine, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx logs every request URL at INFO: 2GIS URLs contain the API key ('?key=...'),
# and website checks would flood the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Initialize database and start bot polling."""
    if settings_error is not None:
        raise SystemExit(f"Invalid configuration in .env / environment: {settings_error}")
    bot_token = settings.BOT_TOKEN.get_secret_value().strip()
    if not bot_token or settings.ADMIN_TG_ID <= 0:
        raise SystemExit("BOT_TOKEN and ADMIN_TG_ID must be set (see .env.example)")

    await init_db()

    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    admin_guard = AdminOnlyMiddleware()
    dp.message.middleware(admin_guard)
    dp.callback_query.middleware(admin_guard)

    dp.include_router(search_router)
    dp.include_router(leads_router)

    logger.info("Bot starting polling for admin ID %s...", settings.ADMIN_TG_ID)
    try:
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
