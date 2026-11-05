"""Entrypoint for SPb Lead Finder Telegram Bot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.bot.handlers.collector import router as collector_router
from src.bot.handlers.issue import router as issue_router
from src.bot.handlers.leads import router as leads_router
from src.bot.handlers.search import router as search_router
from src.bot.middlewares import AdminOnlyMiddleware
from src.bot.notifier import AdminNotifier
from src.config import settings, settings_error
from src.database.repo import async_session_maker, engine, init_db
from src.services.collector import load_collector_config
from src.services.providers.registry import build_providers
from src.services.providers.twogis_scraper import ScraperOptions
from src.services.scheduler import CollectorRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx logs every request URL at INFO: 2GIS URLs contain the API key ('?key=...'),
# and website checks would flood the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
# The collector logs each run itself; APScheduler would add 'Running job...' lines every 15 minutes.
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Initialize database and start bot polling."""
    if settings_error is not None:
        raise SystemExit(f"Invalid configuration in .env / environment: {settings_error}")
    bot_token = settings.BOT_TOKEN.get_secret_value().strip()
    if not bot_token or settings.ADMIN_TG_ID <= 0:
        raise SystemExit("BOT_TOKEN and ADMIN_TG_ID must be set (see .env.example)")
    try:
        collector_config = load_collector_config(settings)
    except ValueError as exc:
        raise SystemExit(f"Invalid configuration in .env / environment: {exc}") from exc

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
    dp.include_router(issue_router)
    dp.include_router(collector_router)

    twogis_key = settings.TWOGIS_API_KEY.get_secret_value() if settings.TWOGIS_API_KEY else None
    provider_set = build_providers(
        collector_config.providers,
        twogis_key,
        ScraperOptions(
            city_slug=settings.SCRAPER_CITY_SLUG,
            min_delay_seconds=settings.SCRAPER_MIN_DELAY_SECONDS,
            max_delay_seconds=settings.SCRAPER_MAX_DELAY_SECONDS,
            max_pages_per_run=settings.SCRAPER_MAX_PAGES_PER_RUN,
            max_results_per_run=settings.SCRAPER_MAX_RESULTS_PER_RUN,
            min_hours_between_runs=settings.SCRAPER_MIN_HOURS_BETWEEN_RUNS,
            page_timeout_seconds=settings.SCRAPER_PAGE_TIMEOUT_SECONDS,
        ),
    )
    for name, reason in provider_set.disabled.items():
        logger.warning("Provider %s is disabled: %s", name, reason)

    collector_runner = CollectorRunner(
        async_session_maker,
        provider_set,
        collector_config,
        report=AdminNotifier(
            bot, settings.admin_ids, settings.notify_chat_ids, settings.NOTIFY_ADMINS_PRIVATE
        ).report,
        enabled_by_default=settings.COLLECT_ENABLED,
        run_on_start=settings.COLLECT_RUN_ON_START,
        min_pool=settings.LEADS_MIN_POOL,
    )
    dp["collector_runner"] = collector_runner
    # Polling calls these inside its own lifetime, while the bot can still send messages.
    dp.startup.register(collector_runner.start)
    dp.shutdown.register(collector_runner.shutdown)

    if not settings.NOTIFY_ADMINS_PRIVATE and not settings.notify_chat_ids:
        logger.warning("NOTIFY_ADMINS_PRIVATE=false needs NOTIFY_CHAT_IDS: without chats admins are still notified")

    logger.info(
        "Bot starting polling for admin IDs %s, notification chats %s...",
        sorted(settings.admin_ids),
        list(settings.notify_chat_ids),
    )
    try:
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
