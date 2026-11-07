"""Tests for contacts from cards and Google as links with their sources, and the ready first message."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.bot import formatters
from src.bot.formatters import MAX_MESSAGE_LENGTH, format_contacts_line, format_issued_lead, outreach_text
from src.config import OUTREACH_TEXT_MAX, Settings
from src.database.models import STATUS_NEW, Lead
from src.services import assisted_runs, lead_enrich
from src.services.contact_links import (
    SOURCE_2GIS,
    SOURCE_GOOGLE,
    ContactLink,
    contact_link,
    lead_contacts,
    links_from_card,
    merge_contact_links,
    parse_stored_links,
)
from src.services.lead_file import (
    ENRICH_GOOGLE_SCHEMA_VERSION,
    FOUND_BY_GOOGLE,
    KIND_ENRICH_RESULT,
    EnrichRequestItem,
    build_enrich_document,
    enrich_result_record,
)
from src.services.lead_import import ImportService
from src.services.site_status import SITE_DEAD, SITE_FOUND, SITE_NO_SITE, SITE_SOCIAL_ONLY, SITE_UNKNOWN

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _card_in(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if "contact_groups" in value:
            return value
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        return None
    for item in values:
        found = _card_in(item)
        if found is not None:
            return found
    return None


def _fixture_card() -> dict[str, Any]:
    card = _card_in(json.loads((FIXTURES / "twogis_state_card.json").read_text(encoding="utf-8")))
    assert card is not None
    return card


def _card(*contacts: dict[str, str]) -> dict[str, Any]:
    return {"id": "70000001", "contact_groups": [{"contacts": list(contacts)}]}


# ---------- reading the contacts of a card ----------


def test_the_fixture_card_gives_its_networks_as_links() -> None:
    """twogis_state_card.json has an email, VK, WhatsApp with a ready text, and a site through link.2gis.ru."""
    assert links_from_card(_fixture_card()) == ["https://vk.com/smile", "https://wa.me/79210000001"]


def test_the_result_record_of_the_fixture_card_has_email_and_networks() -> None:
    item = EnrichRequestItem("2gis", "70000001_abc", "https://2gis.ru/spb/firm/70000001", "70000001")

    record = assisted_runs._result_record(item, _fixture_card(), NOW)

    assert record["parsed"] is True
    assert record["email"] == "info@smile.example"
    assert record["socials"] == ["https://vk.com/smile", "https://wa.me/79210000001"]


def test_networks_missing_from_the_fixtures_are_read_with_fallbacks() -> None:
    """Not seen on 2GIS yet: Telegram and Instagram as nicknames, WhatsApp as a number, a new type with a link."""
    card = _card(
        {"type": "telegram", "text": "Telegram"},  # only the name of the button: nothing to link to
        {"type": "telegram", "value": "@Smile_SPB", "text": "Telegram"},
        {"type": "instagram", "url": "https://www.instagram.com/Smile.Spb/?hl=ru"},
        {"type": "whatsapp", "value": "8 (921) 000-00-02"},
        {"type": "messenger", "value": "https://t.me/smile_chat"},
        {"type": "website", "value": "https://t.me/smile_channel"},
        {"type": "website", "value": "http://link.2gis.ru/1.2/AAAA/online/1/smile"},
        {"type": "youtube", "value": "https://youtube.com/@smile"},
        {"type": "email", "value": "info@smile.example"},
        {"type": "phone", "value": "+7 921 000-00-01"},
    )

    assert links_from_card(card) == [
        "https://t.me/smile_spb",
        "https://instagram.com/smile.spb",
        "https://wa.me/79210000002",
        "https://t.me/smile_chat",
        "https://t.me/smile_channel",
        "https://youtube.com/@smile",
    ]


@pytest.mark.parametrize(
    ("contact_type", "value", "expected"),
    [
        (None, "INFO@Smile.example", "mailto:info@smile.example"),
        ("email", "mailto:info@smile.example?subject=x", "mailto:info@smile.example"),
        ("email", "не почта", None),
        (None, "https://www.vk.com/Smile/", "https://vk.com/smile"),
        ("vkontakte", "club123", "https://vk.com/club123"),
        (None, "vk.ru/smile", "https://vk.com/smile"),
        (None, "https://telegram.me/Smile_spb/12", "https://t.me/smile_spb"),
        ("whatsapp", "+7 921 000-00-01", "https://wa.me/79210000001"),
        (None, "https://api.whatsapp.com/send?phone=79210000001&text=hi", "https://wa.me/79210000001"),
        ("instagram", "smile.spb", "https://instagram.com/smile.spb"),
        (None, "@nickname", None),  # a nickname of what? Without a type nothing
        ("telegram", "", None),
        (None, "javascript:alert(1)", None),
        (None, "не ссылка вовсе", None),
    ],
)
def test_contact_link(contact_type: str | None, value: str, expected: str | None) -> None:
    assert contact_link(contact_type, value) == expected


# ---------- two sources ----------


def test_the_same_link_from_two_sources_is_kept_once_with_both() -> None:
    stored = merge_contact_links(None, ["https://vk.com/smile", "mailto:info@smile.example"], SOURCE_2GIS)
    stored = merge_contact_links(stored, ["https://vk.com/smile/", "https://t.me/smile_spb"], SOURCE_GOOGLE)

    links = parse_stored_links(stored)

    assert [(link.url, link.sources) for link in links] == [
        ("https://vk.com/smile", ("2gis", "google")),
        ("mailto:info@smile.example", ("2gis",)),
        ("https://t.me/smile_spb", ("google",)),
    ]


def test_merging_nothing_keeps_what_was_saved() -> None:
    stored = merge_contact_links(None, ["https://vk.com/smile"], SOURCE_2GIS)

    assert merge_contact_links(stored, [], SOURCE_GOOGLE) == stored
    assert merge_contact_links(None, [], SOURCE_GOOGLE) is None


def test_a_damaged_column_counts_as_nothing_and_foreign_links_are_dropped() -> None:
    assert parse_stored_links("{not json") == []
    stored = json.dumps(
        [{"url": "javascript:alert(1)", "sources": ["2gis"]}, {"url": "https://t.me/a", "sources": ["?"]}]
    )
    assert parse_stored_links(stored) == [ContactLink("https://t.me/a", "telegram", ())]


def test_contacts_saved_before_sources_are_shown_too() -> None:
    stored = merge_contact_links(None, ["https://vk.com/smile"], SOURCE_2GIS)

    links = lead_contacts(stored, "info@smile.example", "https://www.vk.com/smile/, https://taplink.cc/smile")

    assert [link.url for link in links] == [
        "https://vk.com/smile",
        "mailto:info@smile.example",
        "https://taplink.cc/smile",
    ]
    assert links[1].sources == ()


# ---------- importing the files ----------


def _lead(**overrides: Any) -> Lead:
    values: dict[str, Any] = {
        "external_id": "2gis_70001_abc",
        "source": "2gis",
        "name": "Улыбка",
        "category": "dentist_clinic",
        "site_status": SITE_UNKNOWN,
        "score": 40,
        "crm_status": STATUS_NEW,
        "source_url": "https://2gis.ru/spb/firm/70001",
        "created_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return Lead(**values)


def _file(records: list[dict[str, Any]], schema_version: int | None = None) -> bytes:
    kwargs = {"schema_version": schema_version} if schema_version else {}
    return json.dumps(build_enrich_document(KIND_ENRICH_RESULT, records, NOW, **kwargs), ensure_ascii=False).encode()


@pytest.fixture
def service(session_maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportService:
    async def no_network(url, client=None):
        return (SITE_DEAD, "HTTP/Connection error") if url else (SITE_NO_SITE, None)

    monkeypatch.setattr(lead_enrich, "analyze_website", no_network)

    async def report(result, exclude_chat) -> None:
        return None

    return ImportService(session_maker, report, tmp_path / "inbox", 1_000_000, 100)


async def _saved(session_maker) -> Lead:
    async with session_maker() as session:
        lead = await session.get(Lead, 1)
    assert lead is not None
    return lead


async def _add(session_maker, lead: Lead) -> None:
    async with session_maker() as session:
        session.add(lead)
        await session.commit()


async def test_an_old_file_without_networks_imports_as_before(session_maker, service) -> None:
    """A record of an older collector: phones only, socials and email empty."""
    await _add(session_maker, _lead())
    record = enrich_result_record("2gis", "70001_abc", NOW, parsed=True, phones=["+7 921 000-00-01"])

    result = await service.import_bytes(_file([record]), "enrich_old.json", "telegram")

    lead = await _saved(session_maker)
    assert result.ok and result.enrichment is not None and result.enrichment.updated == 1
    assert lead.site_status == SITE_NO_SITE
    assert lead.phone == "+79210000001"
    assert lead.contact_links is None and lead.socials is None and lead.email is None


async def test_a_new_file_saves_networks_and_email_with_the_source(session_maker, service) -> None:
    await _add(session_maker, _lead())
    record = enrich_result_record(
        "2gis",
        "70001_abc",
        NOW,
        parsed=True,
        socials=["https://vk.com/smile", "https://wa.me/79210000001"],
        email="info@smile.example",
    )

    result = await service.import_bytes(_file([record]), "enrich_new.json", "telegram")

    lead = await _saved(session_maker)
    assert result.ok
    assert lead.site_status == SITE_NO_SITE  # a card without a site says so, whatever networks it lists
    assert lead.email == "info@smile.example"
    assert [(link.url, link.sources) for link in parse_stored_links(lead.contact_links)] == [
        ("mailto:info@smile.example", ("2gis",)),
        ("https://vk.com/smile", ("2gis",)),
        ("https://wa.me/79210000001", ("2gis",)),
    ]
    useful = result.enrichment.useful if result.enrichment else []
    assert [link.url for link in useful[0].contacts][:1] == ["mailto:info@smile.example"]


async def test_google_adds_its_contacts_without_erasing_or_doubling_those_of_2gis(session_maker, service) -> None:
    links = merge_contact_links(None, ["https://vk.com/smile", "mailto:info@smile.example"], SOURCE_2GIS)
    await _add(
        session_maker,
        _lead(
            site_status=SITE_NO_SITE, socials="https://vk.com/smile", email="info@smile.example", contact_links=links
        ),
    )
    # Google found nothing new but the same VK written another way, a Telegram and another email.
    record = enrich_result_record(
        "2gis",
        "70001_abc",
        NOW,
        parsed=True,
        socials=["https://www.vk.com/smile/", "https://t.me/smile_spb"],
        email="hello@smile.example",
        found_by=FOUND_BY_GOOGLE,
    )

    result = await service.import_bytes(_file([record], ENRICH_GOOGLE_SCHEMA_VERSION), "google_enrich.json", "telegram")

    lead = await _saved(session_maker)
    assert result.ok
    # The verdict of the card stays: Google found no site, and only its contacts are added.
    assert lead.site_status == SITE_NO_SITE and lead.score == 40
    assert result.enrichment is not None and result.enrichment.ignored_total == 1
    assert lead.email == "info@smile.example"  # the saved one stays in the old column
    assert lead.socials == "https://vk.com/smile, https://t.me/smile_spb"
    assert [(link.url, link.sources) for link in parse_stored_links(lead.contact_links)] == [
        ("https://vk.com/smile", ("2gis", "google")),
        ("mailto:info@smile.example", ("2gis",)),
        ("mailto:hello@smile.example", ("google",)),
        ("https://t.me/smile_spb", ("google",)),
    ]


async def test_an_empty_record_keeps_saved_contacts(session_maker, service) -> None:
    links = merge_contact_links(None, ["https://vk.com/smile"], SOURCE_2GIS)
    await _add(session_maker, _lead(socials="https://vk.com/smile", email="info@smile.example", contact_links=links))
    record = enrich_result_record("2gis", "70001_abc", NOW, parsed=True, website="https://dead.example")

    await service.import_bytes(_file([record]), "enrich.json", "telegram")

    lead = await _saved(session_maker)
    assert lead.site_status == SITE_DEAD
    assert lead.socials == "https://vk.com/smile"
    assert lead.email == "info@smile.example"
    assert lead.contact_links == links


# ---------- showing the contacts ----------


def test_contacts_line_links_each_kind_and_names_the_sources() -> None:
    links = [
        ContactLink("mailto:info@smile.example", "email", ("2gis",)),
        ContactLink("https://t.me/smile_spb", "telegram", ("google",)),
        ContactLink("https://wa.me/79210000001", "whatsapp", ("2gis", "google")),
        ContactLink("https://vk.com/smile", "vk"),
    ]

    line = format_contacts_line(links)

    assert line is not None
    assert '<a href="mailto:info@smile.example">info@smile.example</a> (2ГИС)' in line
    assert '<a href="https://t.me/smile_spb">Telegram @smile_spb</a> (Google)' in line
    assert '<a href="https://wa.me/79210000001">WhatsApp +79210000001</a> (2ГИС, Google)' in line
    assert '<a href="https://vk.com/smile">ВКонтакте smile</a>' in line
    assert format_contacts_line([]) is None


def test_contacts_are_escaped() -> None:
    line = format_contacts_line([ContactLink('https://example.com/a"b<c>', "other")])

    assert line is not None
    assert 'href="https://example.com/a&quot;b&lt;c&gt;"' in line
    assert "<c>" not in line


def test_a_card_without_contacts_has_no_contacts_line() -> None:
    assert "Контакты" not in format_issued_lead(_lead(site_status=SITE_FOUND))


def test_a_card_with_many_long_contacts_stays_within_the_limit() -> None:
    stored = merge_contact_links(None, [f"https://example{n}.com/{'x' * 400}" for n in range(20)], SOURCE_GOOGLE)
    lead = _lead(name="Н" * 255, address="А" * 512, site_status=SITE_NO_SITE, contact_links=stored, phone="1" * 128)

    text = format_issued_lead(lead)

    assert len(text) <= MAX_MESSAGE_LENGTH
    assert "Контакты" in text


# ---------- the first message ----------


@pytest.fixture
def outreach(monkeypatch: pytest.MonkeyPatch):
    def configure(offer: str = "", signature: str = "") -> None:
        values = Settings(_env_file=None, OUTREACH_OFFER=offer, OUTREACH_SIGNATURE=signature)
        monkeypatch.setattr(formatters, "settings", values)

    configure()
    return configure


@pytest.mark.parametrize("status", [SITE_NO_SITE, SITE_SOCIAL_ONLY, "site_not_found"])
def test_a_lead_without_a_site_gets_the_no_site_message(outreach, status: str) -> None:
    outreach("Делаю сайты за неделю.", "Иван, @ivan_dev")

    text = outreach_text(_lead(site_status=status))

    assert text is not None
    assert text.startswith("Здравствуйте! «Улыбка» есть на картах, но сайта у вас найти не удалось.")
    assert "«Стоматологии и клиники»" in text  # the niche without its emoji
    assert "Делаю сайты за неделю." in text and text.endswith("Иван, @ivan_dev")
    assert "{" not in text


def test_a_lead_with_a_dead_site_gets_a_message_that_says_so(outreach) -> None:
    text = outreach_text(_lead(site_status=SITE_DEAD, rubric="Стоматологическая клиника"))

    assert text is not None
    assert "сейчас не открывается" in text
    assert "«Стоматологическая клиника»" in text  # the rubric of 2GIS is preferred


@pytest.mark.parametrize("status", [SITE_FOUND, SITE_UNKNOWN, "site_uncertain"])
def test_other_leads_get_no_message(outreach, status: str) -> None:
    assert outreach_text(_lead(site_status=status)) is None
    assert "Первое сообщение" not in format_issued_lead(_lead(site_status=status))


def test_empty_offer_and_signature_give_a_neutral_text(outreach) -> None:
    text = outreach_text(_lead(site_status=SITE_NO_SITE))

    assert text is not None
    assert "Я делаю сайты для небольших бизнесов" in text
    assert text.endswith("Хорошего дня!")


def test_the_message_is_escaped_and_braces_stay_text(outreach) -> None:
    outreach("Скидка {ниша} <b>50%</b> & {подпись}", "")
    lead = _lead(name="Кафе <b>«{название}»</b>", site_status=SITE_NO_SITE)

    card = format_issued_lead(lead)

    assert "<code>" in card and "</code>" in card
    message = card.split("<code>", 1)[1].split("</code>", 1)[0]
    assert "&lt;b&gt;" in message and "<b>" not in message
    assert "Кафе &lt;b&gt;«{название}»&lt;/b&gt;" in message  # a name with braces is not a placeholder
    assert "Скидка {ниша}" in message and "&amp; {подпись}" in message


def test_long_settings_are_cut_on_start() -> None:
    values = Settings(_env_file=None, OUTREACH_OFFER="слово " * 200, OUTREACH_SIGNATURE="  Иван  \n  @ivan  ")

    assert len(values.OUTREACH_OFFER) == OUTREACH_TEXT_MAX
    assert values.OUTREACH_SIGNATURE == "Иван @ivan"
