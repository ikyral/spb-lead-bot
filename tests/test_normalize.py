"""Tests for normalization of contact data and the name + address duplicate check."""

import pytest

from src.services.dedup import DuplicateIndex, merge_duplicate_places
from src.services.normalize import (
    normalize_address,
    normalize_email,
    normalize_phone,
    normalize_phone_list,
    normalize_place,
    normalize_website,
)
from src.services.providers.base import RawPlace


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (812) 555-01-01", "+78125550101"),
        ("8 812 555-01-01", "+78125550101"),
        ("78125550101", "+78125550101"),
        ("(812) 555-01-01", "+78125550101"),
        ("+7 812 555-01-01 доб. 123", "+78125550101"),
        ("+44 20 7946 0958", "+442079460958"),
        ("+8613800138000", "+8613800138000"),  # China: a leading 8 after '+' is a country code
        ("555-01-01", None),  # no city code: converting would be a guess
        ("+12", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


def test_normalize_phone_list_removes_duplicates_and_keeps_what_cannot_be_converted() -> None:
    assert normalize_phone_list("+7 812 555-01-01, 8 812 5550101; 555-01-01") == "+78125550101, 555-01-01"
    assert normalize_phone_list(" , ") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.RU/?utm_source=2gis&utm_medium=card", "https://example.ru"),
        ("https://example.ru/page/?id=5&utm_campaign=x&yclid=1#top", "https://example.ru/page/?id=5"),
        ("http://example.ru/", "http://example.ru"),
        ("Example.ru/catalog?gclid=abc", "example.ru/catalog"),
        ("  https://vk.com/club1  ", "https://vk.com/club1"),
        ("tel:+78125550101", "tel:+78125550101"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_website(raw: str | None, expected: str | None) -> None:
    assert normalize_website(raw) == expected


def test_normalize_address_and_email() -> None:
    assert normalize_address("  Невский   проспект,  10 , ") == "Невский проспект, 10"
    assert normalize_address("  ,") is None
    assert normalize_email(" Info@Example.RU ") == "info@example.ru"
    assert normalize_email("mailto:info@example.ru") == "info@example.ru"
    assert normalize_email("не почта") is None


def test_normalize_place_does_not_modify_the_original() -> None:
    place = RawPlace(external_id="x", source="2gis", name="  Улыбка  ", category="c", phone="8 812 555-01-01")
    normalized = normalize_place(place)
    assert (normalized.name, normalized.phone) == ("Улыбка", "+78125550101")
    assert place.phone == "8 812 555-01-01"


def _place(external_id: str, name: str, address: str | None = None, phone: str | None = None) -> RawPlace:
    return RawPlace(external_id=external_id, source="2gis", name=name, category="c", address=address, phone=phone)


def test_same_name_and_address_is_one_organization_across_sources() -> None:
    merged = merge_duplicate_places([
        _place("2gis_1", "ООО «Улыбка»", "Невский проспект, 10"),
        _place("yandex_1", "ооо улыбка", "НЕВСКИЙ ПРОСПЕКТ 10", phone="+78125550101"),
    ])
    assert [p.external_id for p in merged] == ["2gis_1"]
    assert merged[0].phone == "+78125550101"


def test_same_name_at_another_address_or_no_address_is_not_a_duplicate() -> None:
    other_address = [_place("a", "Улыбка", "Невский, 10"), _place("b", "Улыбка", "Невский, 12")]
    assert len(merge_duplicate_places(other_address)) == 2
    assert len(merge_duplicate_places([_place("a", "Улыбка"), _place("b", "Улыбка")])) == 2


def test_duplicate_index_finds_by_name_and_address() -> None:
    index = DuplicateIndex()
    index.add(7, "Улыбка", None, None, "Невский, 10")
    assert index.find("УЛЫБКА", None, None, "невский 10") == 7
    assert index.find("Улыбка", None, None, "Невский, 12") is None
