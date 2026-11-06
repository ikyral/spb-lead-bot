"""
The collector for a separate computer: one pass, a local database, a file for the server.

No Telegram here and no bot token needed. It runs the same collection as the bot, with the same
pauses, limits and stops: a block is registered in the local database and honoured by later runs.

--assisted is for a person at the computer: at a captcha the browser window waits for them.

Exit codes: 0 done, 1 error, 2 a source refused or is waiting after a refusal, 130 interrupted.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import Settings, resolve_path, settings, settings_error
from src.database.models import ProviderState
from src.database.repo import enable_provider, get_provider_states, init_db
from src.services.collector import load_collector_config
from src.services.dgis_key import DGIS_PROVIDER_NAME, DgisKeyPolicy, DgisQuota, key_expiry_notice
from src.services.lead_export import ExportResult, export_new_leads
from src.services.providers.registry import build_providers
from src.services.providers.twogis import TwoGisOptions
from src.services.providers.twogis_scraper import ScraperOptions
from src.services.scheduler import STATUS_FAILED, TRIGGER_MANUAL, CollectorRunner, RunReport

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_INTERRUPTED = 130

SCRAPER_PROVIDER_NAME = "2gis_scraper"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сбор лидов на этом компьютере и выгрузка в файл для сервера.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-export", action="store_true", help="только собрать, файл не создавать")
    mode.add_argument("--export-only", action="store_true", help="только выгрузить накопленное, без нового сбора")
    mode.add_argument(
        "--enable-provider",
        metavar="ИСТОЧНИК",
        help="снять автоотключение и паузу источника после блокировок, например 2gis_scraper",
    )
    parser.add_argument(
        "--assisted",
        action="store_true",
        help="парсер 2ГИС с видимым окном браузера: капчу проходит человек за компьютером, сборщик ждёт",
    )
    args = parser.parse_args(argv)
    if args.assisted and (args.export_only or args.enable_provider):
        parser.error("--assisted нужен только для сбора")
    return args


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


@dataclass
class _Outcome:
    """What one launch did, for the console and the exit code."""

    report: RunReport | None = None
    export: ExportResult | None = None
    refused: dict[str, ProviderState] = field(default_factory=dict)
    key_lines: list[str] = field(default_factory=list)


def _print_outcome(outcome: _Outcome) -> None:
    if outcome.report is not None:
        result = outcome.report.result
        print(
            f"Сбор ({outcome.report.status}, {outcome.report.duration_seconds:.0f} с): найдено {result.found}, "
            f"подходит {result.qualified}, новых {result.saved_new}, дубликатов {result.duplicates}, "
            f"ошибок {result.errors}, страниц поиска {result.requests}."
        )
        for name, reason in result.skipped.items():
            print(f"Пропущен {name}: {reason}")
        # Bot commands do not exist here, so the alert points to the option doing the same.
        for alert in result.alerts:
            print(alert.replace("/provider_on", "python collect_cli.py --enable-provider"))
    if outcome.export is not None:
        print(f"Выгружено лидов: {outcome.export.count}.")
        for path in outcome.export.files:
            print(f"Файл: {path}")
    for line in outcome.key_lines:
        print(line)
    for name, state in outcome.refused.items():
        if state.disabled_at is not None:
            cause = "после блокировок " if state.consecutive_blocks else ""
            print(
                f"{name}: отключён {cause}({state.last_block_reason}). "
                f"Включить вручную: python collect_cli.py --enable-provider {name}"
            )
        else:
            print(
                f"{name}: {state.last_block_reason}, повтор не раньше {_local_time(state.blocked_until)}. "
                "Не запускайте сбор повторно: это не поможет и может продлить блокировку."
            )


def _refused_providers(states: dict[str, ProviderState], names: Sequence[str]) -> dict[str, ProviderState]:
    now = datetime.now(UTC)
    return {
        name: state
        for name, state in states.items()
        if name in names
        and (state.disabled_at is not None or (state.blocked_until is not None and now < state.blocked_until))
    }


def _key_lines(state: ProviderState | None, policy: DgisKeyPolicy) -> list[str]:
    """Here nobody gets alerts from the bot: what is left of the API key goes to the console."""
    used = state.requests_total if state is not None else 0
    limit_text = f"из {policy.limit}, осталось {max(0, policy.limit - used)}" if policy.limit else "лимит не задан"
    lines = [f"API 2ГИС: израсходовано запросов {used} {limit_text}."]
    # The owner's local day is what matters here.
    notice = key_expiry_notice(policy, date.today())
    if notice is not None:
        lines.append(notice[1])
    return lines


async def _collect(
    current: Settings, session_maker: async_sessionmaker[AsyncSession], outcome: _Outcome, assisted: bool = False
) -> list[str]:
    """Run one pass of the usual collection. Returns the names of the providers configured here."""
    config = load_collector_config(current.model_copy(update={"COLLECT_PROVIDERS": current.COLLECTOR_PROVIDERS}))
    twogis_key = current.TWOGIS_API_KEY.get_secret_value() if current.TWOGIS_API_KEY else None
    if assisted and SCRAPER_PROVIDER_NAME not in config.providers:
        # Otherwise the run would quietly go without a browser and without the mode that was asked for.
        raise ValueError(
            f"режим с участием человека нужен парсеру: добавьте {SCRAPER_PROVIDER_NAME} в COLLECTOR_PROVIDERS"
        )
    if not current.SCRAPER_HEADLESS and not assisted:
        logger.warning("SCRAPER_HEADLESS=false is for manual debugging only: do not use it on a schedule")
    dgis_quota = (
        DgisQuota(session_maker, twogis_key, DgisKeyPolicy.from_settings(current))
        if twogis_key and twogis_key.strip()
        else None
    )
    if dgis_quota is not None:
        await dgis_quota.sync_key()
    provider_set = build_providers(
        config.providers,
        twogis_key,
        ScraperOptions.from_settings(current, assisted=assisted),
        TwoGisOptions.from_settings(current),
        dgis_quota.spend if dgis_quota else None,
    )

    async def keep_report(report: RunReport) -> None:
        outcome.report = report

    runner = CollectorRunner(session_maker, provider_set, config, keep_report)
    await runner.run_once(TRIGGER_MANUAL)
    return config.providers


async def _run(args: argparse.Namespace, current: Settings) -> int:
    db_path = resolve_path(current.COLLECTOR_DB_PATH)
    await asyncio.to_thread(db_path.parent.mkdir, parents=True, exist_ok=True)
    # This engine is the only one used here; the bot's DB_URL is never connected to.
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(db_path)))
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    outcome = _Outcome()
    try:
        await init_db(engine)

        if args.enable_provider:
            async with session_maker() as session:
                await enable_provider(session, args.enable_provider)
            print(f"Источник {args.enable_provider} включён, счётчик блокировок сброшен.")
            return EXIT_OK

        names: list[str] = []
        if not args.export_only:
            names = await _collect(current, session_maker, outcome, args.assisted or current.SCRAPER_ASSISTED)
        if not args.no_export:
            outcome.export = await export_new_leads(
                session_maker,
                resolve_path(current.COLLECTOR_EXPORT_DIR),
                current.SCRAPER_CITY_SLUG,
                current.IMPORT_MAX_LEADS,
            )
        async with session_maker() as session:
            states = await get_provider_states(session)
        outcome.refused = _refused_providers(states, names)
        if DGIS_PROVIDER_NAME in names and current.TWOGIS_API_KEY:
            outcome.key_lines = _key_lines(states.get(DGIS_PROVIDER_NAME), DgisKeyPolicy.from_settings(current))
    finally:
        await engine.dispose()

    _print_outcome(outcome)
    if outcome.refused:
        return EXIT_BLOCKED
    if outcome.report is not None and outcome.report.status == STATUS_FAILED:
        return EXIT_ERROR
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    # Consoles and redirected logs on Windows use legacy encodings; Cyrillic must survive both.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # stdout: PowerShell turns every line of a program's stderr into an error record.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    if settings_error is not None:
        print(f"Ошибка настроек в .env или окружении: {settings_error}")
        return EXIT_ERROR
    try:
        return asyncio.run(_run(args, settings))
    except ValueError as exc:
        print(f"Ошибка настроек: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Прервано. Браузер закрыт, найденное до этого момента не сохранено.")
        return EXIT_INTERRUPTED

