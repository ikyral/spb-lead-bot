"""Tests for collection settings, the 2GIS page parser and one collection pass."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from src.bot.formatters import format_issued_lead
from src.bot.keyboards import get_issued_lead_keyboard
from src.config import Settings
from src.database.models import CONTACT_CONTACTED, STATUS_ISSUED, Lead, ProviderState
from src.database.repo import get_provider_states, issue_next_leads, set_contact_status
from src.services import pipeline
from src.services.api_client import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderProxyError,
    ProviderQuotaError,
    ProviderResponseError,
    ProviderStoppedError,
    ProviderTemporaryError,
)
from src.services.collector import MAX_CONSECUTIVE_FAILURES, _progress_key, collect_leads, load_collector_config
from src.services.providers import OSMOverpassProvider
from src.services.providers.base import (
    SPB_DISTRICTS,
    TARGET_CATEGORIES,
    BaseMapProvider,
    RawPlace,
    SearchArea,
    SearchPage,
    SearchQuery,
)
from src.services.providers.registry import build_providers
from src.services.providers.twogis_catalog import parse_catalog_items
from src.services.providers.yandex import YandexProvider

FIXTURES = Path(__file__).parent / "fixtures"
MSK = ZoneInfo("Europe/Moscow")
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, **overrides)


def _config(**overrides):
    return load_collector_config(_settings(**overrides))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------- settings ----------


def test_default_schedule_is_daily_at_four_moscow_time() -> None:
    config = _config()
    fire_time = config.trigger.get_next_fire_time(None, datetime(2026, 11, 5, 12, 0, tzinfo=MSK))
    assert fire_time == datetime(2026, 11, 6, 4, 0, tzinfo=MSK)
    assert config.timezone == MSK


@pytest.mark.parametrize(
    ("cron", "after", "expected"),
    [
        # Saturday -> Monday: cron's 1-5 is Mon-Fri, not APScheduler's Tue-Sat.
        ("0 9 * * 1-5", datetime(2026, 11, 7, 12, 0, tzinfo=MSK), datetime(2026, 11, 9, 9, 0, tzinfo=MSK)),
        # Saturday -> Sunday: both 0 and 7 mean Sunday in cron.
        ("0 9 * * 0", datetime(2026, 11, 7, 12, 0, tzinfo=MSK), datetime(2026, 11, 8, 9, 0, tzinfo=MSK)),
        ("0 9 * * 7", datetime(2026, 11, 7, 12, 0, tzinfo=MSK), datetime(2026, 11, 8, 9, 0, tzinfo=MSK)),
        ("30 8 * * mon", datetime(2026, 11, 7, 12, 0, tzinfo=MSK), datetime(2026, 11, 9, 8, 30, tzinfo=MSK)),
    ],
)
def test_cron_weekdays_follow_the_crontab_convention(cron: str, after: datetime, expected: datetime) -> None:
    assert _config(COLLECT_CRON=cron).trigger.get_next_fire_time(None, after) == expected


def test_interval_replaces_cron_and_timezone_is_configurable() -> None:
    config = _config(COLLECT_INTERVAL_MINUTES=360, COLLECT_TIMEZONE="Asia/Yekaterinburg")
    assert config.trigger.interval.total_seconds() == 6 * 3600
    assert config.timezone == ZoneInfo("Asia/Yekaterinburg")
    assert "360" in config.schedule_text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"COLLECT_CRON": "сегодня"}, "COLLECT_CRON"),
        ({"COLLECT_CRON": "99 99 * * *"}, "COLLECT_CRON"),
        ({"COLLECT_TIMEZONE": "Марс/Олимп"}, "COLLECT_TIMEZONE"),
        ({"COLLECT_PROVIDERS": "2gis,google"}, "COLLECT_PROVIDERS"),
        ({"COLLECT_DISTRICTS": "Гагаринский"}, "COLLECT_DISTRICTS"),
        ({"COLLECT_BBOX": "1,2,3"}, "COLLECT_BBOX"),
        ({"COLLECT_BBOX": "60,30,59,31"}, "COLLECT_BBOX"),
    ],
)
def test_invalid_settings_name_the_variable(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _config(**overrides)


def test_default_targets_are_all_categories_in_all_districts() -> None:
    config = _config()
    phrases = sum(len(category["queries_2gis"]) for category in TARGET_CATEGORIES.values())
    assert len(config.targets) == phrases * len(SPB_DISTRICTS)
    assert config.providers == ["2gis_cdp", "osm"]  # the browser reader of 2GIS is the main source
    assert len(config.targets) == 470  # 47 searches (a niche may have several) in 10 districts
    assert {"pet_services", "fitness_studios", "furniture_custom", "kids_centers"} <= {
        target.query.category for target in config.targets
    }


def test_progress_keys_are_unique_and_the_old_targets_keep_theirs(monkeypatch: pytest.MonkeyPatch) -> None:
    new_niches = {
        "pet_services",
        "fitness_studios",
        "furniture_custom",
        "kids_centers",
        *("shoe_repair", "key_making", "clothing_repair", "watch_repair", "phone_repair"),
        *("brows", "dry_cleaning", "street_food", "framing", "bike_repair"),
    }
    provider = FakeProvider([])
    provider.name = "2gis"
    targets = _config().targets
    keys = [_progress_key(provider, target) for target in targets]
    assert len(set(keys)) == len(targets) == 470
    old_keys = {_progress_key(provider, t) for t in targets if t.query.category not in new_niches}
    assert len(old_keys) == 270  # niches that existed before the ten small-business ones
    # The same targets built without the new niches give the very same keys.
    for key in new_niches:
        monkeypatch.delitem(TARGET_CATEGORIES, key)
    without_new = {_progress_key(provider, t) for t in _config().targets}
    assert without_new == old_keys


def test_targets_from_settings_with_free_text_query_and_bbox() -> None:
    config = _config(COLLECT_QUERIES="auto_repair, кондитерская", COLLECT_BBOX="59.9, 30.2, 60.0, 30.5")
    queries = [target.query for target in config.targets]
    phrases = TARGET_CATEGORIES["auto_repair"]["queries_2gis"]
    assert [query.category for query in queries] == ["auto_repair"] * len(phrases) + ["кондитерская"]
    assert [query.text for query in queries[: len(phrases)]] == list(phrases)
    assert queries[0].osm_tags and all(query.osm_tags == () for query in queries[1 : len(phrases)])
    assert queries[-1].text == "кондитерская" and queries[-1].osm_tags == ()
    assert config.targets[0].area == SearchArea(bbox=(59.9, 30.2, 60.0, 30.5))


def test_empty_values_in_env_mean_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLLECT_INTERVAL_MINUTES", "")
    assert _settings().COLLECT_INTERVAL_MINUTES is None


async def test_yandex_stub_never_returns_data() -> None:
    assert await YandexProvider().search_places("Центральный", "beauty") == []
    with pytest.raises(Exception, match="Яндекс"):
        await YandexProvider().search(QUERY, AREA)


# ---------- 2GIS page parsing ----------


def _catalog_places(name: str) -> list[RawPlace]:
    """What the browser providers get out of a card of 2GIS: the pure parser on a saved response."""
    return parse_catalog_items(_fixture(name), "Центральный", "dentist_clinic", True)


def test_twogis_items_from_fixture_keep_only_valid_organizations() -> None:
    places = _catalog_places("twogis_items.json")

    assert [place.external_id for place in places] == [
        "2gis_70000001018420390_abc123def",
        "2gis_70000001018420391",
        "2gis_70000001018420393",
    ]
    first = places[0]
    assert (first.source, first.name, first.category, first.district, first.high_margin) == (
        "2gis",
        "Улыбка, стоматология",
        "dentist_clinic",
        "Центральный",
        True,
    )
    assert first.email == "Info@Smile-Spb.ru"
    assert first.source_url == "https://2gis.ru/spb/firm/70000001018420390"
    assert first.map_url == "https://yandex.ru/maps/?pt=30.325,59.935&z=17&l=map"
    assert first.socials == "https://vk.com/smile_spb"

    odd = places[2]
    assert (odd.phone, odd.raw_website, odd.map_url) == (None, None, None)


async def test_overpass_search_reports_total_failure_and_skips_free_text() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    provider = OSMOverpassProvider(client=client)
    query = SearchQuery(text="", category="beauty", osm_tags=(("shop", "beauty"),))
    with pytest.raises(ProviderTemporaryError):
        await provider.search(query, AREA)
    assert (await provider.search(QUERY, AREA)).places == []  # free text has no OSM tags


async def test_overpass_places_link_to_their_osm_card() -> None:
    payload = {"elements": [{"type": "way", "id": 5, "center": {"lat": 59.9, "lon": 30.3}, "tags": {"name": "Салон"}}]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    query = SearchQuery(text="", category="beauty", osm_tags=(("shop", "beauty"),))
    page = await OSMOverpassProvider(client=client).search(query, AREA)
    assert page.places[0].source_url == "https://www.openstreetmap.org/way/5"
    assert page.has_more is False


# ---------- one collection pass ----------


def _place(number: int, source: str = "2gis", phone: str | None = None, **fields) -> RawPlace:
    return RawPlace(
        external_id=f"{source}_{number}",
        source=source,
        name=f"Организация {number}",
        category="dentist_clinic",
        phone=phone or f"+7 812 555-{number:02d}-{number:02d}",
        high_margin=True,
        **fields,
    )


class FakeProvider(BaseMapProvider):
    """Serves prepared pages or raises prepared errors, and records requests."""

    name = "fake"

    def __init__(self, pages=None, error: Exception | None = None, name: str = "fake") -> None:
        self.pages = pages if pages is not None else []
        self.error = error
        self.calls: list[tuple[str, int]] = []
        self.name = name

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls.append((query.category, page))
        if self.error is not None:
            raise self.error
        if page > len(self.pages):
            return SearchPage(places=[])
        return SearchPage(places=self.pages[page - 1], has_more=page < len(self.pages))


class WarningProvider(BaseMapProvider):
    """Serves one page of places with a warning about the data, like a source that answers without contacts."""

    name = "warning"

    def __init__(self, places: list[RawPlace], warning: str) -> None:
        self.places = places
        self.warning = warning

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        return SearchPage(places=self.places if page == 1 else [], warning=self.warning)


@pytest.fixture
def no_site_checks(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Website checks are not under test: everything counts as having no site, calls are recorded."""
    checked: list[str | None] = []

    async def fake_analyze(url, client=None):
        checked.append(url)
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)
    return checked


def _one_target(**overrides):
    defaults = {"COLLECT_QUERIES": "dentist_clinic", "COLLECT_DISTRICTS": "Центральный"}
    return _config(**{**defaults, **overrides})


async def _all_leads(session_maker) -> list[Lead]:
    async with session_maker() as session:
        return list((await session.scalars(select(Lead).order_by(Lead.id))).all())


async def test_collection_paginates_normalizes_and_saves(session_maker, no_site_checks) -> None:
    provider = FakeProvider(pages=[[_place(1, phone="8 812 555-01-01", email="Info@A.ru")], [_place(2)]])
    result = await collect_leads(session_maker, [provider], _one_target())

    assert provider.calls == [("dentist_clinic", 1), ("dentist_clinic", 2)]
    assert (result.found, result.qualified, result.saved_new, result.duplicates, result.errors) == (2, 2, 2, 0, 0)
    assert result.requests == 2
    leads = await _all_leads(session_maker)
    assert (leads[0].phone, leads[0].email, leads[0].crm_status) == ("+78125550101", "info@a.ru", "new")


async def test_page_limit_is_respected(session_maker, no_site_checks) -> None:
    provider = FakeProvider(pages=[[_place(i)] for i in range(1, 6)])
    await collect_leads(session_maker, [provider], _one_target(COLLECT_MAX_PAGES=2))
    assert len(provider.calls) == 2


async def test_result_limit_per_query_is_respected(session_maker, no_site_checks) -> None:
    provider = FakeProvider(pages=[[_place(i) for i in range(1, 6)], [_place(6)]])
    result = await collect_leads(session_maker, [provider], _one_target(COLLECT_MAX_RESULTS_PER_QUERY=3))
    assert result.found == 3
    assert len(provider.calls) == 1


async def test_duplicates_inside_one_result_are_saved_once(session_maker, no_site_checks) -> None:
    provider = FakeProvider(pages=[[_place(1), _place(1)], [_place(1), _place(2)]])
    result = await collect_leads(session_maker, [provider], _one_target())
    assert result.saved_new == 2
    assert len(await _all_leads(session_maker)) == 2


async def test_same_organization_from_two_sources_is_one_lead(session_maker, no_site_checks) -> None:
    first = FakeProvider(pages=[[_place(1, source="2gis", phone="+7 812 555-01-01")]], name="2gis")
    second = FakeProvider(pages=[[_place(9, source="osm", phone="8 812 5550101")]], name="osm")
    result = await collect_leads(session_maker, [first, second], _one_target())
    assert result.saved_new == 1
    assert [lead.source for lead in await _all_leads(session_maker)] == ["2gis"]


async def test_second_run_creates_no_duplicates_and_checks_no_known_sites(session_maker, no_site_checks) -> None:
    pages = [[_place(1, raw_website="https://a.example"), _place(2, raw_website="https://b.example")]]
    await collect_leads(session_maker, [FakeProvider(pages=pages)], _one_target())
    assert len(no_site_checks) == 2

    no_site_checks.clear()
    result = await collect_leads(session_maker, [FakeProvider(pages=pages)], _one_target())

    assert (result.found, result.saved_new, result.duplicates) == (2, 0, 2)
    assert no_site_checks == []
    assert len(await _all_leads(session_maker)) == 2


async def test_found_again_lead_is_neither_reissued_nor_overwritten(session_maker, no_site_checks) -> None:
    pages = [[_place(1), _place(2)]]
    await collect_leads(session_maker, [FakeProvider(pages=pages)], _one_target())

    async with session_maker() as session:
        issued = await issue_next_leads(session, 2)
        await set_contact_status(session, issued[0].id, CONTACT_CONTACTED, 1, "@anna")
    issued_at = {lead.id: lead.issued_at for lead in issued}

    changed = [_place(1, phone="+7 812 555-01-01", address="Новый адрес"), _place(2)]
    result = await collect_leads(session_maker, [FakeProvider(pages=[changed])], _one_target())

    assert result.saved_new == 0
    leads = await _all_leads(session_maker)
    assert [lead.crm_status for lead in leads] == [STATUS_ISSUED, STATUS_ISSUED]
    assert [lead.contact_status for lead in leads] == [CONTACT_CONTACTED, None]
    assert leads[0].address is None
    assert {lead.id: lead.issued_at for lead in leads} == issued_at
    async with session_maker() as session:
        assert await issue_next_leads(session, 10) == []


async def test_website_owners_are_not_leads(session_maker, monkeypatch: pytest.MonkeyPatch) -> None:
    async def has_site(url, client=None):
        return "has_site", None

    monkeypatch.setattr(pipeline, "analyze_website", has_site)
    result = await collect_leads(session_maker, [FakeProvider(pages=[[_place(1)]])], _one_target())
    assert (result.found, result.qualified, result.saved_new) == (1, 0, 0)


async def test_empty_results_are_not_an_error(session_maker, no_site_checks) -> None:
    result = await collect_leads(session_maker, [FakeProvider(pages=[])], _one_target())
    assert (result.found, result.saved_new, result.errors, result.alerts) == (0, 0, 0, [])


@pytest.mark.parametrize(
    "error",
    [ProviderAuthError("2ГИС: код 403. Проверьте ключ"), ProviderQuotaError("2ГИС: код 429. Квота")],
)
async def test_key_and_quota_errors_stop_the_provider_and_alert(session_maker, no_site_checks, error) -> None:
    failing = FakeProvider(error=error, name="2gis")
    healthy = FakeProvider(pages=[[_place(1)]], name="osm")
    config = _config(COLLECT_QUERIES="dentist_clinic,barbershop", COLLECT_DISTRICTS="Центральный")

    result = await collect_leads(session_maker, [failing, healthy], config)

    assert len(failing.calls) == 1  # never repeated
    assert len(healthy.calls) == 2  # the other source keeps working
    assert result.alerts == [str(error)]
    assert result.last_error == str(error)
    assert result.saved_new == 1


async def test_single_temporary_error_does_not_alert(session_maker, no_site_checks) -> None:
    failing = FakeProvider(error=ProviderTemporaryError("сеть"))
    result = await collect_leads(session_maker, [failing], _one_target())
    assert (result.errors, result.alerts) == (1, [])


async def test_provider_failing_again_and_again_is_skipped_with_one_alert(session_maker, no_site_checks) -> None:
    failing = FakeProvider(error=ProviderTemporaryError("сеть"))
    config = _config(COLLECT_QUERIES="dentist_clinic", COLLECT_DISTRICTS="")
    result = await collect_leads(session_maker, [failing], config)
    assert len(failing.calls) == MAX_CONSECUTIVE_FAILURES
    assert result.errors == MAX_CONSECUTIVE_FAILURES
    assert len(result.alerts) == 1
    assert "сеть" in result.alerts[0]


async def test_provider_bug_does_not_stop_the_collection(session_maker, no_site_checks) -> None:
    broken = FakeProvider(error=KeyError("bug"), name="broken")
    healthy = FakeProvider(pages=[[_place(1)]], name="osm")
    result = await collect_leads(session_maker, [broken, healthy], _one_target())
    assert result.errors == 1
    assert result.saved_new == 1


async def test_results_before_a_failure_are_kept(session_maker, no_site_checks) -> None:
    class FailsOnSecondPage(FakeProvider):
        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            if page == 2:
                raise ProviderTemporaryError("сеть")
            return SearchPage(places=[_place(1)], has_more=True)

    result = await collect_leads(session_maker, [FailsOnSecondPage()], _one_target())
    assert (result.saved_new, result.errors) == (1, 1)


async def test_data_warning_becomes_a_single_alert(session_maker, no_site_checks, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.settings, "LEADS_REQUIRE_CONTACT", True)
    provider = WarningProvider(_catalog_places("twogis_no_contacts.json"), "нет доступа к contact_groups")
    config = _config(COLLECT_QUERIES="dentist_clinic,barbershop", COLLECT_DISTRICTS="Центральный")
    result = await collect_leads(session_maker, [provider], config)
    assert len(result.alerts) == 1
    assert "contact_groups" in result.alerts[0]
    assert result.saved_new == 0  # no phone and no site: below the score threshold


async def test_request_budget_is_never_exceeded_and_the_next_run_continues(session_maker, no_site_checks) -> None:
    config = _config(COLLECT_QUERIES="", COLLECT_DISTRICTS="Центральный", COLLECT_MAX_REQUESTS_PER_RUN=2)
    categories = [target.query.category for target in config.targets]

    first = FakeProvider(pages=[[_place(1)]])
    result = await collect_leads(session_maker, [first], config)
    assert [call[0] for call in first.calls] == categories[:2]
    assert (result.requests, result.next_cursor) == (2, 2)

    second = FakeProvider(pages=[[_place(2)]])
    result = await collect_leads(session_maker, [second], config, cursor=result.next_cursor)
    assert [call[0] for call in second.calls] == categories[2:4]
    assert result.next_cursor == 4

    # Separate provider name so ProviderState('fake').cursor from the first two runs does not override `cursor`.
    third = FakeProvider(pages=[[_place(3)]], name="wrap")
    await collect_leads(session_maker, [third], config, cursor=len(categories) - 1)
    assert [call[0] for call in third.calls] == [categories[-1], categories[0]]  # wraps around


async def test_budget_argument_overrides_the_run_limit(session_maker, no_site_checks) -> None:
    provider = FakeProvider(pages=[[_place(1)]])
    result = await collect_leads(session_maker, [provider], _config(), max_requests=1)
    assert result.requests == 1


async def test_from_2gis_response_to_the_card_the_admin_receives(session_maker, no_site_checks) -> None:
    """The whole way: a card of 2GIS, normalization, storage, batch, message."""
    provider = FakeProvider(pages=[_catalog_places("twogis_items.json")])
    result = await collect_leads(session_maker, [provider], _one_target(COLLECT_MAX_PAGES=1))
    assert result.errors == 0
    assert result.saved_new == 3  # the third has no contacts: a lead too, handed out after the others

    async with session_maker() as session:
        batch = await issue_next_leads(session, 30)
    assert batch[0].phone == "+78125550101"
    assert batch[0].email == "info@smile-spb.ru"
    assert batch[0].raw_website == "https://www.smile-spb.ru/?page=1"
    assert batch[0].address == "Невский проспект, 10"

    card = format_issued_lead(batch[0])
    assert "Улыбка, стоматология" in card
    assert "Стоматологии и клиники" in card
    assert "+78125550101" in card
    assert "смотри в карточке" not in card
    button = get_issued_lead_keyboard(batch[0]).inline_keyboard[0][0]
    assert button.url == "https://2gis.ru/spb/firm/70000001018420390"
    assert format_issued_lead(batch[2]).count("смотри в карточке") == 1


def test_items_without_contacts_are_still_parsed() -> None:
    places = _catalog_places("twogis_no_contacts.json")
    assert len(places) == 5
    assert not any(place.contacts_known for place in places)


def test_env_example_is_a_valid_configuration() -> None:
    """A copy of .env.example must start the bot and match the documented defaults."""
    example = Path(__file__).parent.parent / ".env.example"
    settings = Settings(_env_file=example)
    config = load_collector_config(settings)
    assert Settings.model_fields["COLLECT_CRON"].default == settings.COLLECT_CRON
    assert settings.COLLECT_INTERVAL_MINUTES is None
    assert (settings.LEADS_BATCH_SIZE, settings.LEADS_MIN_POOL) == (30, 60)
    assert config.providers == ["2gis_cdp", "osm"]


def test_registry_explains_why_a_provider_is_off() -> None:
    result = build_providers(["osm", "yandex"])
    assert [provider.name for provider in result.providers] == ["osm"]
    assert "Яндекс" in result.disabled["yandex"]


def test_every_niche_has_short_separate_searches() -> None:
    seen: dict[str, str] = {}
    for key, category in TARGET_CATEGORIES.items():
        phrases = category["queries_2gis"]
        assert isinstance(phrases, tuple) and phrases, key
        for phrase in phrases:
            assert phrase.strip() == phrase and 0 < len(phrase.split()) <= 3, (key, phrase)  # no glued-together lists
            assert phrase not in seen, f"{phrase!r} is searched for both by {seen[phrase]} and {key}"
            seen[phrase] = key


def test_a_niche_gives_one_target_per_phrase_and_district_and_only_the_first_reads_osm() -> None:
    config = _config(COLLECT_QUERIES="fitness_studios", COLLECT_DISTRICTS="Центральный,Невский")
    phrases = TARGET_CATEGORIES["fitness_studios"]["queries_2gis"]
    assert len(config.targets) == len(phrases) * 2
    first_district = [target.query for target in config.targets if target.area.district == "Центральный"]
    assert [query.text for query in first_district] == list(phrases)
    assert first_district[0].osm_tags and all(query.osm_tags == () for query in first_district[1:])
    assert {query.category for query in first_district} == {"fitness_studios"}


async def test_providers_keep_independent_cursors_when_one_stops_on_budget(session_maker, no_site_checks) -> None:
    """When 2gis_cdp stops on budget early and osm keeps going, 2gis_cdp does not skip targets on the next run."""

    class BudgetLimitedCdp(BaseMapProvider):
        name = "2gis_cdp"

        def __init__(self, remaining_targets: int) -> None:
            self.remaining_targets = remaining_targets
            self.completed: list[str] = []

        async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
            return []

        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            if self.remaining_targets <= 0:
                raise ProviderBudgetExhausted("дневной лимит карточек")
            self.remaining_targets -= 1
            self.completed.append(query.category)
            return SearchPage(places=[_place(len(self.completed), source="2gis")])

    config = _config(
        COLLECT_QUERIES="dentist_clinic,barbershop,tire_service,florists,cleaning",
        COLLECT_DISTRICTS="Центральный",
        COLLECT_MAX_REQUESTS_PER_RUN=4,
    )
    categories = [target.query.category for target in config.targets]
    assert len(categories) == 5

    cdp1 = BudgetLimitedCdp(remaining_targets=1)
    osm1 = FakeProvider(pages=[[_place(10, source="osm")]], name="osm")
    first = await collect_leads(session_maker, [cdp1, osm1], config)
    assert cdp1.completed == [categories[0]]
    assert [call[0] for call in osm1.calls] == categories[:2]
    assert first.next_cursor == 2

    async with session_maker() as session:
        states = await get_provider_states(session)
    assert states["2gis_cdp"].cursor == 1
    assert states["osm"].cursor == 2

    cdp2 = BudgetLimitedCdp(remaining_targets=1)
    osm2 = FakeProvider(pages=[[_place(20, source="osm")]], name="osm")
    await collect_leads(session_maker, [cdp2, osm2], config, cursor=first.next_cursor)
    assert cdp2.completed == [categories[1]]
    assert [call[0] for call in osm2.calls] == categories[2:4]

    async with session_maker() as session:
        states = await get_provider_states(session)
    assert states["2gis_cdp"].cursor == 2
    assert states["osm"].cursor == 4


async def test_skipped_provider_with_null_cursor_is_not_dragged_forward(session_maker, no_site_checks) -> None:
    """A resting/blocked provider whose cursor is still None freezes at start instead of following CollectorState."""
    config = _config(
        COLLECT_QUERIES="dentist_clinic,barbershop,tire_service,florists,cleaning",
        COLLECT_DISTRICTS="Центральный",
        COLLECT_MAX_REQUESTS_PER_RUN=2,
    )
    categories = [target.query.category for target in config.targets]
    async with session_maker() as session:
        session.add(ProviderState(name="2gis_cdp", consecutive_blocks=0, last_run_at=datetime.now(UTC), cursor=None))
        await session.commit()

    resting_cdp = FakeProvider(pages=[[_place(1, source="2gis")]], name="2gis_cdp")
    resting_cdp.min_hours_between_runs = 20.0
    osm = FakeProvider(pages=[[_place(2, source="osm")]], name="osm")

    first = await collect_leads(session_maker, [resting_cdp, osm], config, cursor=1)
    assert resting_cdp.calls == []
    assert [call[0] for call in osm.calls] == categories[1:3]
    assert first.next_cursor == 3

    async with session_maker() as session:
        states = await get_provider_states(session)
    assert states["2gis_cdp"].cursor == 1
    assert states["osm"].cursor == 3

    # Once the rest ends, 2gis_cdp resumes at target 1 even though `cursor=first.next_cursor` is 3.
    resting_cdp.ignore_interval = True
    second = await collect_leads(
        session_maker,
        [resting_cdp, FakeProvider(pages=[[_place(3, source="osm")]], name="osm")],
        config,
        cursor=first.next_cursor,
        max_requests=2,
    )
    assert resting_cdp.calls == [(categories[1], 1)]
    assert second.errors == 0


async def test_collect_leads_passes_known_external_ids_by_prefix(session_maker, no_site_checks) -> None:
    async with session_maker() as session:
        session.add_all(
            [
                Lead(
                    external_id="2gis_101",
                    source="2gis",
                    name="Клиника 101",
                    category="dentist_clinic",
                    site_status="no_site",
                    score=70,
                ),
                Lead(
                    external_id="2gis_102",
                    source="2gis",
                    name="Клиника 102",
                    category="dentist_clinic",
                    site_status="no_site",
                    score=70,
                ),
                Lead(
                    external_id="osm_201",
                    source="osm",
                    name="OSM 201",
                    category="dentist_clinic",
                    site_status="no_site",
                    score=70,
                ),
            ]
        )
        await session.commit()

    class HintProvider(FakeProvider):
        external_id_prefix = "2gis_"

        def __init__(self, name: str) -> None:
            super().__init__(pages=[], name=name)
            self.received_hints: list[set[str]] = []

        def set_known_external_ids(self, external_ids: set[str]) -> None:
            self.received_hints.append(set(external_ids))
            external_ids.add("mutated_locally")

    class PlainProvider(FakeProvider):
        def __init__(self, name: str) -> None:
            super().__init__(pages=[], name=name)
            self.received = False

        def set_known_external_ids(self, external_ids: set[str]) -> None:
            self.received = True

    first_hint = HintProvider("2gis_cdp")
    second_hint = HintProvider("2gis_copy")
    plain = PlainProvider("osm")

    await collect_leads(session_maker, [first_hint, second_hint, plain], _one_target())

    assert first_hint.received_hints == [{"2gis_101", "2gis_102"}]
    # Second provider with the same prefix receives a fresh set without the first provider's mutation.
    assert second_hint.received_hints == [{"2gis_101", "2gis_102"}]
    assert plain.received is False


_CURSOR_QUERIES = "dentist_clinic,barbershop,tire_service"


def _cursor_config(**overrides):
    return _config(COLLECT_QUERIES=_CURSOR_QUERIES, COLLECT_DISTRICTS="Центральный", **overrides)


async def _cursor_of(session_maker, name: str) -> int | None:
    async with session_maker() as session:
        return (await get_provider_states(session))[name].cursor


async def test_format_error_on_first_page_moves_the_cursor_on(session_maker, no_site_checks) -> None:
    config = _cursor_config()
    categories = [target.query.category for target in config.targets]
    broken = FakeProvider(error=ProviderFormatError("5 карточек подряд не прочитаны"), name="2gis_cdp")
    first = await collect_leads(session_maker, [broken], config)
    assert broken.calls == [(categories[0], 1)]
    assert first.errors == 1
    assert await _cursor_of(session_maker, "2gis_cdp") == 1

    again = FakeProvider(pages=[[_place(1)]], name="2gis_cdp")
    await collect_leads(session_maker, [again], config, cursor=first.next_cursor)
    assert again.calls[0] == (categories[1], 1)


@pytest.mark.parametrize(
    "error",
    [
        ProviderBlockedError("captcha"),
        ProviderProxyError("proxy"),
        ProviderNoConnectionError("no connection"),
    ],
)
async def test_block_proxy_and_connection_errors_keep_the_cursor(session_maker, no_site_checks, error) -> None:
    config = _cursor_config()
    provider = FakeProvider(error=error, name="2gis_cdp")
    await collect_leads(session_maker, [provider], config)
    assert await _cursor_of(session_maker, "2gis_cdp") == 0


async def test_budget_exhausted_before_a_target_keeps_the_cursor(session_maker, no_site_checks) -> None:
    config = _cursor_config()
    provider = FakeProvider(error=ProviderBudgetExhausted("лимит"), name="2gis_cdp")
    await collect_leads(session_maker, [provider], config)
    assert await _cursor_of(session_maker, "2gis_cdp") == 0


async def test_a_target_with_a_page_and_then_an_error_moves_the_cursor_on(session_maker, no_site_checks) -> None:
    class PageThenFormatError(FakeProvider):
        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            if page == 1:
                return SearchPage(places=[_place(1)], has_more=True)
            raise ProviderFormatError("карточки не прочитаны")

    config = _cursor_config(COLLECT_MAX_REQUESTS_PER_RUN=2)
    await collect_leads(session_maker, [PageThenFormatError(name="2gis_cdp")], config)
    assert await _cursor_of(session_maker, "2gis_cdp") == 1


async def test_temporary_error_keeps_the_cursor(session_maker, no_site_checks, caplog) -> None:
    config = _cursor_config()
    provider = FakeProvider(error=ProviderTemporaryError("сеть"), name="2gis_cdp")
    with caplog.at_level("WARNING"):
        result = await collect_leads(session_maker, [provider], config)
    assert result.errors == MAX_CONSECUTIVE_FAILURES
    categories = [target.query.category for target in config.targets]
    # The same target is retried: the cursor did not move inside the run either.
    assert [category for category, _ in provider.calls] == [categories[0]] * MAX_CONSECUTIVE_FAILURES
    assert await _cursor_of(session_maker, "2gis_cdp") == 0
    assert len([r for r in caplog.records if "failed on" in r.getMessage()]) == MAX_CONSECUTIVE_FAILURES


@pytest.mark.parametrize(
    "error",
    [ProviderResponseError("bad answer"), KeyError("bug"), ProviderStoppedError("stopped")],
)
async def test_target_bound_errors_move_the_cursor_on(session_maker, no_site_checks, error) -> None:
    config = _cursor_config(COLLECT_MAX_REQUESTS_PER_RUN=1)
    provider = FakeProvider(error=error, name="2gis_cdp")
    result = await collect_leads(session_maker, [provider], config)
    assert result.errors == 1
    assert await _cursor_of(session_maker, "2gis_cdp") == 1


async def test_temporary_error_on_the_first_target_keeps_the_cursor_with_one_request(
    session_maker, no_site_checks
) -> None:
    config = _cursor_config(COLLECT_MAX_REQUESTS_PER_RUN=1)
    provider = FakeProvider(error=ProviderTemporaryError("сеть"), name="2gis_cdp")
    result = await collect_leads(session_maker, [provider], config)
    assert result.errors == 1
    assert await _cursor_of(session_maker, "2gis_cdp") == 0
