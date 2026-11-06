"""Tests for telling leads that need a website from those that do not: statuses, verdicts, order, chains, niches."""

import pytest

from src.bot.formatters import format_issued_lead, format_lead_card, generate_pitch_text
from src.database.models import Lead
from src.services.site_status import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    ISSUE_GROUPS,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNCERTAIN,
    SITE_UNKNOWN,
    SOURCE_MANUAL,
    confidence_of_check,
    issue_group,
    source_of_status,
    yandex_search_url,
)


def _lead(**overrides) -> Lead:
    values = {
        "id": 1,
        "external_id": "2gis_1",
        "source": "2gis",
        "name": "Улыбка",
        "category": "dentist_clinic",
        "site_status": SITE_UNKNOWN,
        "score": 50,
        "crm_status": "new",
    }
    return Lead(**{**values, **overrides})


# ---------- what a card says ----------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"site_status": SITE_NO_SITE}, "🌐 Сайта нет (по данным источника)"),
        ({"site_status": SITE_NO_SITE, "site_source": SOURCE_MANUAL}, "✅ Сайта нет (проверено вручную)"),
        ({"site_status": SITE_NOT_FOUND}, "🌐 Сайт не найден в поиске"),
        ({"site_status": SITE_UNCERTAIN, "site_url": "example.ru"}, "🌐 Возможно, сайт: example.ru, проверьте"),
        ({"site_status": SITE_UNKNOWN}, "🌐 Статус неизвестен"),
        ({"site_status": SITE_FOUND, "site_url": "example.ru"}, "🌐 Сайт есть (example.ru)"),
    ],
)
@pytest.mark.parametrize("card", [format_issued_lead, format_lead_card])
def test_card_says_how_much_is_known(card, overrides: dict, expected: str) -> None:
    assert expected in card(_lead(**overrides))


@pytest.mark.parametrize("status", [SITE_UNKNOWN, SITE_NOT_FOUND, SITE_UNCERTAIN])
@pytest.mark.parametrize("show", [format_issued_lead, format_lead_card, generate_pitch_text])
def test_nothing_claims_there_is_no_site_unless_it_is_confirmed(show, status: str) -> None:
    text = show(_lead(site_status=status)).lower()
    assert "сайта нет" not in text
    assert "нет сайта" not in text
    assert "нет собственного" not in text


def test_candidate_address_cannot_break_the_markup() -> None:
    card = format_issued_lead(_lead(site_status=SITE_UNCERTAIN, site_url="<b>x</b>.ru"))
    assert "&lt;b&gt;x&lt;/b&gt;.ru" in card


def test_a_person_can_tell_the_owner_more_than_a_source_can() -> None:
    assert "нет собственного сайта" in generate_pitch_text(_lead(site_status=SITE_NO_SITE, site_source=SOURCE_MANUAL))
    assert "нет собственного сайта" not in generate_pitch_text(_lead(site_status=SITE_NO_SITE, site_source="osm"))


# ---------- what the statuses mean ----------


def test_order_of_groups() -> None:
    assert ISSUE_GROUPS[0] == (SITE_NO_SITE, SITE_DEAD, SITE_SOCIAL_ONLY)
    positions = [issue_group(status) for status in (SITE_DEAD, SITE_NOT_FOUND, SITE_UNCERTAIN, SITE_UNKNOWN)]
    assert positions == sorted(positions)
    assert len(set(positions)) == 4


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        (SITE_DEAD, "osm", CONFIDENCE_HIGH),
        (SITE_SOCIAL_ONLY, "2gis", CONFIDENCE_HIGH),
        (SITE_NO_SITE, "2gis", CONFIDENCE_MEDIUM),
        (SITE_NO_SITE, "osm", CONFIDENCE_LOW),  # OSM often just lacks the tag
        (SITE_UNKNOWN, "2gis", None),
    ],
)
def test_confidence_of_a_check(status: str, source: str, expected: str | None) -> None:
    assert confidence_of_check(status, source) == expected


def test_only_known_sources_are_named() -> None:
    assert source_of_status("2gis") == "2gis"
    assert source_of_status("osm") == "osm"
    assert source_of_status("csv") is None


def test_search_link_is_an_ordinary_encoded_link() -> None:
    url = yandex_search_url("Кафе & Бар", "Невский, 1")
    assert url.startswith("https://yandex.ru/search/?text=")
    assert " " not in url and "&" not in url.removeprefix("https://yandex.ru/search/?")
    assert "%D0%A1%D0%B0%D0%BD%D0%BA%D1%82" in yandex_search_url("Кафе", None)  # the city, when no address is known
    assert len(yandex_search_url("я" * 1000, "б" * 1000)) < 1500
