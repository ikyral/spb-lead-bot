"""Tests for the owner deciding when to run by hand: no interval, or skipping it once. Nothing else may give way."""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src import collector_cli
from src.config import Settings
from src.database.models import ProviderState
from src.database.repo import get_provider_states, init_db
from src.services import pipeline
from src.services.assisted_runs import ENRICH_MAX_CARDS
from src.services.collector import REST_REASON, interval_ends_at, unavailable_reason
from src.services.lead_file import KIND_ENRICH_REQUEST, build_enrich_document
from src.services.providers.registry import ProviderSet
from src.services.providers.twogis_scraper import CAPTCHA_POLL_SECONDS, ScraperOptions, TwoGisScraperProvider
from src.services.scraper.browser import PageSnapshot
from src.services.scraper.robots import RobotsRules, parse_robots

FIXTURES = Path(__file__).parent / "fixtures"
CAPTCHA = PageSnapshot(
    status=200,
    final_url="https://captcha.2gis.ru/form?return_url=x",
    title="2GIS Captcha",
    html=(FIXTURES / "twogis_captcha.html").read_text(encoding="utf-8"),
)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WARNING = "skipped by the decision of the owner"


def _card(number: int = 1) -> PageSnapshot:
    firm = f"7000{number}"
    item = {
        "id": firm,
        "type": "branch",
        "name": "Организация",
        "contact_groups": [{"contacts": [{"type": "phone", "value": "+79210000001"}]}],
    }
    state = {"data": {"entity": {"profile": {firm: {"data": item}}}}}
    return PageSnapshot(
        status=200,
        final_url=f"https://2gis.ru/spb/firm/{firm}",
        title="Организация",
        html="<html></html>",
        page_state={"initialState": state},
    )


def _search() -> PageSnapshot:
    state = {
        "data": {
            "search": {"profile": {"s": {"data": {"total": 0}}}, "pagination": {}},
            "entity": {"profile": {}},
        }
    }
    return PageSnapshot(
        status=200, final_url="https://2gis.ru/spb/search/x", title="Поиск", html="", page_state={"initialState": state}
    )


class Session:
    def __init__(self, pages: list[PageSnapshot]) -> None:
        self.pages = list(pages)
        self.visited: list[str] = []

    async def start(self) -> None:
        return None

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        self.visited.append(url)
        return self.pages.pop(0)

    async def read_page(self) -> PageSnapshot | None:
        return _card()

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        return None


class Time:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay

    def clock(self) -> float:
        return self.now


def _request(tmp_path: Path, count: int = 1) -> str:
    records = [
        {
            "source": "2gis",
            "source_id": f"7000{number}",
            "source_url": f"https://2gis.ru/spb/firm/7000{number}",
            "name": "Организация",
            "district": None,
        }
        for number in range(1, count + 1)
    ]
    path = tmp_path / "enrich_request.json"
    path.write_text(json.dumps(build_enrich_document(KIND_ENRICH_REQUEST, records, NOW)), encoding="utf-8")
    return str(path)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The command line with a real provider, a browser of the test and a temporary database."""

    class Holder:
        pass

    holder = Holder()
    holder.tmp = tmp_path
    holder.session = Session([])
    holder.time = Time()
    holder.options = []
    holder.robots = RobotsRules()
    holder.settings = Settings(
        _env_file=None,
        COLLECTOR_DB_PATH=str(tmp_path / "data" / "collector.db"),
        COLLECTOR_EXPORT_DIR=str(tmp_path / "exports"),
        COLLECTOR_PROVIDERS="2gis_scraper",
        COLLECT_QUERIES="dentist_clinic",
        COLLECT_DISTRICTS="Центральный",
        COLLECT_MAX_PAGES=1,
    )
    monkeypatch.setattr(collector_cli, "settings", holder.settings)
    monkeypatch.setattr(collector_cli, "settings_error", None)

    def provider(options: ScraperOptions) -> TwoGisScraperProvider:
        holder.options.append(options)

        async def rules() -> RobotsRules:
            return holder.robots

        return TwoGisScraperProvider(
            options=options,
            session_factory=lambda: holder.session,
            robots_loader=rules,
            sleep=holder.time.sleep,
            clock=holder.time.clock,
            notify=lambda _text: None,
        )

    monkeypatch.setattr(collector_cli, "_scraper_provider", provider)
    monkeypatch.setattr(
        collector_cli,
        "build_providers",
        lambda names, key, options=None, *rest: ProviderSet(providers=[provider(options)]),
    )

    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)
    return holder


def _on_database(tmp_path: Path, action):
    async def run():
        engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "data" / "collector.db")))
        await init_db(engine)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            result = await action(session)
        await engine.dispose()
        return result

    (tmp_path / "data").mkdir(exist_ok=True)
    return asyncio.run(run())


def _set_state(tmp_path: Path, **values) -> None:
    async def write(session):
        session.add(ProviderState(name="2gis_scraper", **{"consecutive_blocks": 0, **values}))
        await session.commit()

    _on_database(tmp_path, write)


def _state(tmp_path: Path) -> ProviderState:
    async def read(session):
        return (await get_provider_states(session))["2gis_scraper"]

    return _on_database(tmp_path, read)


def _recent(hours: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


# ---------- the setting ----------


def test_zero_in_the_settings_is_accepted_and_the_default_stays() -> None:
    assert Settings(_env_file=None, SCRAPER_MIN_HOURS_BETWEEN_RUNS=0).SCRAPER_MIN_HOURS_BETWEEN_RUNS == 0
    assert Settings(_env_file=None, SCRAPER_MIN_HOURS_BETWEEN_RUNS=0.5).SCRAPER_MIN_HOURS_BETWEEN_RUNS == 0.5
    assert Settings(_env_file=None).SCRAPER_MIN_HOURS_BETWEEN_RUNS == 20.0


def test_a_negative_interval_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SCRAPER_MIN_HOURS_BETWEEN_RUNS=-1)


def test_zero_means_no_interval(cli, capsys) -> None:
    cli.settings.SCRAPER_MIN_HOURS_BETWEEN_RUNS = 0.0
    _set_state(cli.tmp, last_run_at=_recent())
    cli.session = Session([_card()])

    assert collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)]) == collector_cli.EXIT_OK

    assert len(cli.session.visited) == 1
    assert "прочитано 1" in capsys.readouterr().out


def test_zero_still_keeps_a_pause_after_a_block(cli, capsys) -> None:
    cli.settings.SCRAPER_MIN_HOURS_BETWEEN_RUNS = 0.0
    _set_state(cli.tmp, blocked_until=datetime.now(UTC) + timedelta(hours=1), last_block_reason="капча")

    assert collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)]) == collector_cli.EXIT_BLOCKED

    assert cli.session.visited == []
    assert "пауза после блокировки" in capsys.readouterr().out


# ---------- the check of the interval ----------


def _bare_provider(**options) -> TwoGisScraperProvider:
    return TwoGisScraperProvider(options=ScraperOptions(**options))


def test_the_check_is_skipped_only_with_the_flag() -> None:
    state = ProviderState(name="2gis_scraper", consecutive_blocks=0, last_run_at=NOW - timedelta(hours=1))

    assert unavailable_reason(_bare_provider(), state, NOW) == REST_REASON
    assert unavailable_reason(_bare_provider(ignore_interval=True), state, NOW) is None


def test_the_end_of_the_interval_is_known() -> None:
    state = ProviderState(name="2gis_scraper", consecutive_blocks=0, last_run_at=NOW)

    assert interval_ends_at(_bare_provider(min_hours_between_runs=20.0), state) == NOW + timedelta(hours=20)
    assert interval_ends_at(_bare_provider(min_hours_between_runs=0.0), state) is None
    assert interval_ends_at(_bare_provider(), None) is None


@pytest.mark.parametrize(
    "state",
    [
        ProviderState(name="x", consecutive_blocks=1, blocked_until=NOW + timedelta(hours=1), last_block_reason="k"),
        ProviderState(name="x", consecutive_blocks=3, disabled_at=NOW, last_block_reason="k"),
    ],
)
def test_the_flag_does_not_remove_a_pause_or_a_switch_off(state: ProviderState) -> None:
    assert unavailable_reason(_bare_provider(ignore_interval=True), state, NOW) is not None


def test_the_bot_never_skips_the_interval() -> None:
    assert ScraperOptions.from_settings(Settings(_env_file=None)).ignore_interval is False
    assert ScraperOptions.from_settings(Settings(_env_file=None), assisted=True).ignore_interval is False


# ---------- without the flag: as before, with a hint ----------


def test_without_the_flag_the_interval_holds_and_the_message_helps(cli, capsys) -> None:
    last_run = _recent()
    _set_state(cli.tmp, last_run_at=last_run)

    assert collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)]) == collector_cli.EXIT_BLOCKED

    assert cli.session.visited == []
    out = capsys.readouterr().out
    assert "Карточки не открывались: уже запускался сегодня" in out
    allowed = (last_run + timedelta(hours=20)).astimezone().strftime("%d.%m.%Y %H:%M")
    assert f"По интервалу можно запустить после {allowed}" in out
    assert "--assisted --enrich ФАЙЛ --ignore-interval" in out
    state = _state(cli.tmp)
    assert state.last_run_at == last_run


def test_a_collection_without_the_flag_is_skipped_with_the_same_hint(cli, capsys) -> None:
    last_run = _recent()
    _set_state(cli.tmp, last_run_at=last_run)

    collector_cli.main(["--assisted", "--no-export"])

    assert cli.session.visited == []
    out = capsys.readouterr().out
    allowed = (last_run + timedelta(hours=20)).astimezone().strftime("%d.%m.%Y %H:%M")
    assert f"По интервалу можно запустить после {allowed}" in out
    assert "--assisted --ignore-interval" in out


def test_a_pause_does_not_get_the_hint_about_the_flag(cli, capsys) -> None:
    _set_state(cli.tmp, blocked_until=datetime.now(UTC) + timedelta(hours=1), last_block_reason="капча")

    collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)])

    assert "--ignore-interval" not in capsys.readouterr().out


# ---------- with the flag ----------


def test_the_flag_reads_cards_warns_and_records_the_run(cli, capsys, caplog) -> None:
    last_run = _recent()
    _set_state(cli.tmp, last_run_at=last_run)
    cli.session = Session([_card()])

    with caplog.at_level(logging.WARNING):
        code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_OK
    assert len(cli.session.visited) == 1
    assert WARNING in caplog.text
    assert _state(cli.tmp).last_run_at > last_run
    assert cli.options[0].min_hours_between_runs == 20.0  # the interval itself stays, for the next run


def test_the_next_run_waits_for_the_interval_again(cli) -> None:
    _set_state(cli.tmp, last_run_at=_recent())
    request = _request(cli.tmp)
    cli.session = Session([_card()])
    assert collector_cli.main(["--assisted", "--enrich", request, "--ignore-interval"]) == collector_cli.EXIT_OK

    cli.session = Session([])
    assert collector_cli.main(["--assisted", "--enrich", request]) == collector_cli.EXIT_BLOCKED

    assert cli.session.visited == []


def test_the_flag_works_for_a_collection_and_records_the_run(cli, caplog) -> None:
    last_run = _recent()
    _set_state(cli.tmp, last_run_at=last_run)
    cli.session = Session([_search()])

    with caplog.at_level(logging.WARNING):
        collector_cli.main(["--assisted", "--no-export", "--ignore-interval"])

    assert len(cli.session.visited) == 1
    assert WARNING in caplog.text
    assert _state(cli.tmp).last_run_at > last_run


def test_no_warning_without_the_flag(cli, caplog) -> None:
    cli.session = Session([_card()])

    with caplog.at_level(logging.WARNING):
        collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)])

    assert WARNING not in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        ["--ignore-interval"],
        ["--no-export", "--ignore-interval"],
        ["--export-only", "--ignore-interval"],
        ["--enable-provider", "2gis_scraper", "--ignore-interval"],
    ],
)
def test_the_flag_needs_assisted(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        collector_cli.main(argv)

    assert stopped.value.code == 2
    assert "--assisted" in capsys.readouterr().err


def test_the_setting_of_assisted_is_not_enough_for_the_flag(cli) -> None:
    cli.settings.SCRAPER_ASSISTED = True

    with pytest.raises(SystemExit):
        collector_cli.main(["--ignore-interval"])


def test_the_flag_has_no_business_with_check(capsys) -> None:
    with pytest.raises(SystemExit):
        collector_cli.main(["--assisted", "--check", "--ignore-interval"])

    assert "--check" in capsys.readouterr().err


# ---------- with the flag: everything else stays ----------


def test_the_flag_does_not_remove_a_pause_from_the_command_line(cli, capsys) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    _set_state(cli.tmp, consecutive_blocks=1, blocked_until=until, last_block_reason="капча")

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_BLOCKED
    assert cli.session.visited == []
    assert "пауза после блокировки" in capsys.readouterr().out
    state = _state(cli.tmp)
    assert state.consecutive_blocks == 1
    assert state.blocked_until == until
    assert state.last_run_at is None


def test_the_flag_does_not_switch_a_source_back_on(cli, capsys) -> None:
    now = datetime.now(UTC)
    _set_state(cli.tmp, consecutive_blocks=3, disabled_at=now, last_block_reason="капча")

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_BLOCKED
    assert cli.session.visited == []
    assert "отключён" in capsys.readouterr().out
    assert _state(cli.tmp).disabled_at is not None


def test_the_flag_does_not_remove_a_pause_of_a_collection(cli) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    _set_state(cli.tmp, consecutive_blocks=1, blocked_until=until, last_block_reason="k")

    code = collector_cli.main(["--assisted", "--no-export", "--ignore-interval"])

    assert code == collector_cli.EXIT_BLOCKED
    assert cli.session.visited == []


def test_a_successful_run_with_the_flag_does_not_reset_the_counter(cli) -> None:
    _set_state(cli.tmp, consecutive_blocks=2, blocked_until=_recent(), last_run_at=_recent(2), last_block_reason="k")
    cli.session = Session([_card()])

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_OK
    assert _state(cli.tmp).consecutive_blocks == 2


def test_a_collection_with_the_flag_does_not_reset_the_counter(cli) -> None:
    _set_state(cli.tmp, consecutive_blocks=2, blocked_until=_recent(), last_run_at=_recent(2), last_block_reason="k")
    cli.session = Session([_search()])

    collector_cli.main(["--assisted", "--no-export", "--ignore-interval"])

    assert len(cli.session.visited) == 1
    assert _state(cli.tmp).consecutive_blocks == 2


def test_a_run_without_the_flag_still_resets_it_as_before(cli) -> None:
    _set_state(cli.tmp, consecutive_blocks=2, blocked_until=_recent(), last_block_reason="k")
    cli.session = Session([_card()])

    assert collector_cli.main(["--assisted", "--enrich", _request(cli.tmp)]) == collector_cli.EXIT_OK

    assert _state(cli.tmp).consecutive_blocks == 0


def test_a_block_during_a_run_with_the_flag_counts(cli, capsys) -> None:
    _set_state(cli.tmp, consecutive_blocks=1, blocked_until=_recent(), last_run_at=_recent(2), last_block_reason="k")
    refused = replace(_card(), status=403)
    cli.session = Session([refused])

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_BLOCKED
    state = _state(cli.tmp)
    assert state.consecutive_blocks == 2
    assert state.blocked_until > datetime.now(UTC) + timedelta(hours=11)  # the pause doubled to 12 hours
    assert "403" in capsys.readouterr().out


def test_the_third_block_with_the_flag_switches_the_source_off(cli) -> None:
    _set_state(cli.tmp, consecutive_blocks=2, blocked_until=_recent(), last_block_reason="k")
    cli.session = Session([replace(_card(), status=429)])

    collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert _state(cli.tmp).disabled_at is not None


def test_the_flag_keeps_the_limit_of_cards_and_the_pauses(cli) -> None:
    count = ENRICH_MAX_CARDS + 5
    cli.session = Session([_card(number) for number in range(1, count + 1)])

    collector_cli.main(["--assisted", "--enrich", _request(cli.tmp, count), "--ignore-interval"])

    assert len(cli.session.visited) == ENRICH_MAX_CARDS
    pauses = [delay for delay in cli.time.delays if delay != CAPTCHA_POLL_SECONDS]
    assert len(pauses) == ENRICH_MAX_CARDS - 1
    assert all(3.0 <= pause <= 8.0 for pause in pauses)


def test_the_flag_keeps_robots_txt(cli, capsys) -> None:
    cli.robots = parse_robots("User-agent: *\nDisallow: */firm/", "spb-lead-bot")

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_ERROR
    assert cli.session.visited == []
    assert "robots" in capsys.readouterr().out


def test_a_captcha_nobody_passes_with_the_flag_is_a_block(cli) -> None:
    class Waiting(Session):
        async def read_page(self) -> PageSnapshot | None:
            return CAPTCHA

    cli.session = Waiting([CAPTCHA])

    code = collector_cli.main(["--assisted", "--enrich", _request(cli.tmp), "--ignore-interval"])

    assert code == collector_cli.EXIT_BLOCKED
    assert _state(cli.tmp).consecutive_blocks == 1
