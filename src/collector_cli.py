"""
The collector for a separate computer: one pass, a local database, a file for the server.

No Telegram here and no bot token needed. It runs the same collection as the bot, with the same
pauses, limits and stops: a block is registered in the local database and honoured by later runs.

--assisted is for a person at the computer: at a captcha the browser window waits for them.
With it, --check looks at one live page to see if the parser understands the site, and --enrich
reads the cards of leads the bot asked about, for a file to send back to the bot.

Exit codes: 0 done, 1 error, 2 a source refused or is waiting after a refusal, 130 interrupted.
"""

import argparse
import asyncio
import logging
import re
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import Settings, SettingsError, resolve_path, settings, settings_error
from src.database.models import ProviderState
from src.database.repo import enable_provider, get_provider_states, init_db
from src.services.assisted_runs import CHECK_MAX_PAGES, ENRICH_MAX_CARDS, CheckReport, EnrichRun, run_check, run_enrich
from src.services.collector import REST_REASON, CollectorConfig, load_collector_config
from src.services.dgis_key import DGIS_PROVIDER_NAME, DgisKeyPolicy, DgisQuota, key_expiry_notice
from src.services.lead_export import ExportResult, export_new_leads
from src.services.lead_file import LeadFileError, parse_enrich_request
from src.services.lead_import import read_limited
from src.services.providers.registry import build_providers
from src.services.providers.twogis import TwoGisOptions
from src.services.providers.twogis_scraper import ScraperOptions, TwoGisScraperProvider
from src.services.scheduler import STATUS_FAILED, TRIGGER_MANUAL, CollectorRunner, RunReport

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_INTERRUPTED = 130

SCRAPER_PROVIDER_NAME = "2gis_scraper"
CHECK_LOG_PATH = "logs/scraper_check.json"
SAMPLES_DIR = "data/samples"
ERROR_LOG_PATH = "logs/collector_errors.log"

_HIDDEN = "<скрыто>"
# The query of a link holds API keys and search words, the part before '@' a password of a database.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"?]*)\?[^\s'\"]*")
_URL_PASSWORD_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


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
    mode.add_argument(
        "--check",
        action="store_true",
        help="вместе с --assisted: проверить парсер на одной странице поиска и одной карточке, ничего не сохраняя",
    )
    mode.add_argument(
        "--enrich",
        metavar="ФАЙЛ",
        help="вместе с --assisted: прочитать карточки из файла команды /export_unknown и записать файл для бота",
    )
    parser.add_argument(
        "--save-sample",
        action="store_true",
        help="вместе с --check: сохранить страницы и их состояние в data/samples, чтобы разбирать формат без сайта",
    )
    parser.add_argument(
        "--ignore-interval",
        action="store_true",
        help="вместе с --assisted: запустить сейчас, не дожидаясь интервала. Паузу после блокировки не снимает",
    )
    parser.add_argument(
        "--assisted",
        action="store_true",
        help="парсер 2ГИС с видимым окном браузера: капчу проходит человек за компьютером, сборщик ждёт",
    )
    args = parser.parse_args(argv)
    if args.assisted and (args.export_only or args.enable_provider):
        parser.error("--assisted нужен только для сбора")
    if args.ignore_interval and not args.assisted:
        parser.error("--ignore-interval работает только вместе с --assisted")
    if args.ignore_interval and args.check:
        parser.error("--check не зависит от интервала, у него свой лимит: --ignore-interval здесь не нужен")
    if args.save_sample and not args.check:
        parser.error("--save-sample работает только вместе с --check")
    if (args.check or args.enrich is not None) and not args.assisted:
        parser.error("--check и --enrich работают только вместе с --assisted: капчу проходит человек")
    return args


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _rest_hint(retry_at: datetime | None, command: str) -> str:
    """Whether to run now is for the owner to decide: say when the interval ends and how not to wait for it."""
    return (
        f"По интервалу можно запустить после {_local_time(retry_at)}. "
        f"Запустить сейчас: python collect_cli.py {command} --ignore-interval"
    )


@dataclass
class _Outcome:
    """What one launch did, for the console and the exit code."""

    report: RunReport | None = None
    export: ExportResult | None = None
    refused: dict[str, ProviderState] = field(default_factory=dict)
    key_lines: list[str] = field(default_factory=list)
    # When the interval ends, for the providers skipped because of it.
    rest_until: dict[str, datetime] = field(default_factory=dict)


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
            if reason == REST_REASON:
                print(_rest_hint(outcome.rest_until.get(name), "--assisted"))
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
    current: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    outcome: _Outcome,
    assisted: bool = False,
    ignore_interval: bool = False,
) -> list[str]:
    """Run one pass of the usual collection. Returns the names of the providers configured here."""
    config = _collector_config(current)
    twogis_key = current.TWOGIS_API_KEY.get_secret_value() if current.TWOGIS_API_KEY else None
    if assisted and SCRAPER_PROVIDER_NAME not in config.providers:
        # Otherwise the run would quietly go without a browser and without the mode that was asked for.
        raise SettingsError(
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
        replace(ScraperOptions.from_settings(current, assisted=assisted), ignore_interval=ignore_interval),
        TwoGisOptions.from_settings(current),
        dgis_quota.spend if dgis_quota else None,
    )

    async def keep_report(report: RunReport) -> None:
        outcome.report = report

    runner = CollectorRunner(session_maker, provider_set, config, keep_report)
    await runner.run_once(TRIGGER_MANUAL)
    return config.providers


def _collector_config(current: Settings) -> CollectorConfig:
    return load_collector_config(current.model_copy(update={"COLLECT_PROVIDERS": current.COLLECTOR_PROVIDERS}))


def _value_or_reason(value: object, reason: str) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return str(value) if value else reason


def _print_check(report: CheckReport) -> None:
    if report.refused is not None:
        retry = f" Следующая возможна после {_local_time(report.retry_at)}." if report.retry_at else ""
        print(f"Проверка не запущена: {report.refused}.{retry}")
        return
    if report.search_found is not None:
        print(f"Поиск: организаций в ответе {report.search_found}.")
    if report.search_problem:
        print(f"Поиск: {report.search_problem}.")
    if report.card_problem:
        print(f"Карточка: {report.card_problem}.")
    if report.blocked:
        print(f"Сайт отказал: {report.blocked}. Это записано как блокировка, повтор после паузы.")
    if report.failed:
        print(f"Проверка остановлена: {report.failed}.")
    found = report.recognized
    # Without a recognized card, an empty field says only that nothing was understood.
    absent = "нет в карточке" if found.get("contacts_recognized") else "не распознано"
    for title, key, reason in (
        ("Название", "name", "не распознано"),
        ("Адрес", "address", "не указан или не распознан"),
        ("Телефоны", "phones", absent),
        ("Сайт", "website", absent),
        ("Соцсети", "socials", absent),
        ("Ссылка", "source_url", "не распознана"),
    ):
        print(f"  {title}: {_value_or_reason(found.get(key), reason)}")
    if report.path is not None:
        print(f"Файл с найденным и структурой ответов: {report.path}")
    for path in report.sample_files:
        print(f"Образец: {path}")
    print(f"Проверок осталось за 24 часа: {report.runs_left}.")


async def _check(current: Settings, session_maker: async_sessionmaker[AsyncSession], save_sample: bool) -> int:
    target = _collector_config(current).targets[0]
    options = replace(
        ScraperOptions.from_settings(current, assisted=True),
        max_pages_per_run=CHECK_MAX_PAGES,
        min_hours_between_runs=0.0,
    )
    report = await run_check(
        session_maker,
        _scraper_provider(options),
        target,
        resolve_path(CHECK_LOG_PATH),
        current.SCRAPER_BLOCK_BACKOFF_HOURS,
        options.city_slug,
        current.SCRAPER_CHECK_MAX_PER_DAY,
        resolve_path(SAMPLES_DIR) if save_sample else None,
    )
    _print_check(report)
    if report.refused or report.blocked:
        return EXIT_BLOCKED
    return EXIT_ERROR if report.failed else EXIT_OK


def _print_enrich(run: EnrichRun) -> None:
    if run.refused == REST_REASON:
        print(f"Карточки не открывались: {run.refused}. {_rest_hint(run.retry_at, '--assisted --enrich ФАЙЛ')}")
        return
    if run.refused is not None:
        print(f"Карточки не открывались: {run.refused}. Не запускайте повторно раньше времени.")
        return
    print(
        f"Карточек прочитано {run.read}, не распознано {len(run.records) - run.read}, "
        f"не открыто {run.not_opened + run.over_limit}."
    )
    if run.over_limit:
        print(f"За запуск открывается не больше {ENRICH_MAX_CARDS} карточек, остальные {run.over_limit} не открыты.")
    for reason, count in run.rejected.most_common():
        print(f"Записей пропущено ({reason}): {count}.")
    if run.blocked:
        print(f"Сайт отказал: {run.blocked}. Это записано как блокировка, повтор после паузы.")
    if run.stopped:
        print(f"Чтение остановлено: {run.stopped}.")
    if run.failed:
        print(f"Чтение остановлено: {run.failed}.")
    print(f"Отправьте боту файл: {run.path}" if run.path else "Файл не создан: прочитать ничего не удалось.")


async def _enrich(
    current: Settings, session_maker: async_sessionmaker[AsyncSession], file_name: str, ignore_interval: bool = False
) -> int:
    _collector_config(current)  # the same check of the settings as before a collection
    try:
        data = await asyncio.to_thread(read_limited, Path(file_name), current.IMPORT_MAX_FILE_BYTES)
        request = parse_enrich_request(data, current.IMPORT_MAX_FILE_BYTES)
    except OSError as exc:
        print(f"Файл запроса не прочитан: {exc.strerror or type(exc).__name__}.")
        return EXIT_ERROR
    except LeadFileError as exc:
        print(f"Файл запроса не принят: {exc}.")
        return EXIT_ERROR

    run = EnrichRun()
    options = replace(ScraperOptions.from_settings(current, assisted=True), ignore_interval=ignore_interval)
    try:
        await run_enrich(
            session_maker,
            _scraper_provider(options),
            request,
            resolve_path(current.COLLECTOR_EXPORT_DIR),
            current.SCRAPER_BLOCK_BACKOFF_HOURS,
            run,
        )
    finally:
        # Also when interrupted: what was read is in the file, and the person has to know where.
        _print_enrich(run)
    if run.refused or run.blocked:
        return EXIT_BLOCKED
    return EXIT_ERROR if run.failed else EXIT_OK


def _scraper_provider(options: ScraperOptions) -> TwoGisScraperProvider:
    return TwoGisScraperProvider(options=options)


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

        if args.ignore_interval:
            logger.warning("The interval between runs is skipped by the decision of the owner (--ignore-interval)")
        if args.check:
            return await _check(current, session_maker, args.save_sample)
        if args.enrich is not None:
            return await _enrich(current, session_maker, args.enrich, args.ignore_interval)

        names: list[str] = []
        if not args.export_only:
            names = await _collect(
                current, session_maker, outcome, args.assisted or current.SCRAPER_ASSISTED, args.ignore_interval
            )
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
        if outcome.report is not None:
            hours = timedelta(hours=current.SCRAPER_MIN_HOURS_BETWEEN_RUNS)
            for name, reason in outcome.report.result.skipped.items():
                last_run = states[name].last_run_at if name in states else None
                if reason == REST_REASON and last_run is not None:
                    outcome.rest_until[name] = last_run + hours
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


def _without_secrets(text: str, current: Settings) -> str:
    """What an error says may repeat a key or a link with it, and this text is printed and stays in a file."""
    for secret in (current.BOT_TOKEN, current.TWOGIS_API_KEY, current.DGIS_API_KEY):
        value = secret.get_secret_value().strip() if secret else ""
        if value:
            text = text.replace(value, _HIDDEN)
    text = _URL_PASSWORD_RE.sub(f"://{_HIDDEN}@", text)
    return _URL_QUERY_RE.sub(rf"\1?{_HIDDEN}", text)


def _report_unexpected(exc: Exception, current: Settings) -> None:
    """A bug or a failure nobody foresaw: say what it is, and keep the details for whoever fixes it."""
    print(f"Непредвиденная ошибка: {type(exc).__name__}: {_without_secrets(str(exc), current)}")
    details = _without_secrets("".join(traceback.format_exception(exc)), current)
    path = resolve_path(ERROR_LOG_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')}\n{details}\n")
    except OSError as write_error:
        print(f"Подробности не записаны: {write_error.strerror or type(write_error).__name__}.")
        return
    print(f"Подробности: {path}")


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
    except SettingsError as exc:
        print(f"Ошибка настроек: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        if args.enrich is not None:
            print("Прервано. Браузер закрыт, прочитанные карточки сохранены в файл.")
        elif args.check:
            print("Прервано. Браузер закрыт.")
        else:
            print("Прервано. Браузер закрыт, найденное до этого момента не сохранено.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - the last line of defense: reported and saved, with the traceback
        _report_unexpected(exc, settings)
        return EXIT_ERROR

