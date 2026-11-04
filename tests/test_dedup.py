"""Unit tests for cross-source duplicate detection (OSM / 2GIS / CSV)."""

import pytest

from src.services.dedup import (
    DuplicateIndex,
    coords_from_map_url,
    distance_m,
    merge_duplicate_places,
    normalize_name,
    normalize_phones,
)
from src.services.providers.base import RawPlace


def _map_url(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (812) 555-01-01", {"8125550101"}),
        ("8 812 5550101", {"8125550101"}),
        ("78125550101", {"8125550101"}),
        ("+7 812 555-01-01, +7 (999) 111-22-33", {"8125550101", "9991112233"}),
        ("+7 812 555-01-01;+7 999 111 22 33", {"8125550101", "9991112233"}),
        ("+7 (812) 555-01-01 доб. 123", {"8125550101"}),
        ("123", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_normalize_phones(raw: str | None, expected: set[str]) -> None:
    assert normalize_phones(raw) == expected


def test_normalize_name() -> None:
    assert normalize_name("Улыбка, стоматологическая клиника") == normalize_name("«Улыбка»")
    assert normalize_name('Кафе "Ёлка"') == normalize_name("кафе елка")
    assert normalize_name("  ") == ""
    assert normalize_name("Улыбка") != normalize_name("Улыбка Плюс")


def test_coords_and_distance() -> None:
    assert coords_from_map_url(_map_url(59.93, 30.31)) == (59.93, 30.31)
    assert coords_from_map_url("https://yandex.ru/maps/org/123") is None
    assert coords_from_map_url(None) is None
    # ~111 m per 0.001 deg of latitude
    assert 100 < distance_m((59.930, 30.31), (59.931, 30.31)) < 120


def test_merge_by_shared_phone_fills_missing_fields() -> None:
    osm = RawPlace(
        external_id="osm_1", source="osm", name="Улыбка", category="dentist_clinic",
        phone="+7 812 555-01-01", map_url=_map_url(59.93, 30.31),
    )
    gis = RawPlace(
        external_id="2gis_1", source="2gis", name="Улыбка, стоматология", category="dentist_clinic",
        address="Невский проспект, 22", phone="8 (812) 555-01-01, +7 999 111-22-33",
        raw_website="https://ulybka.ru", socials="https://vk.com/ulybka", high_margin=True,
    )
    merged = merge_duplicate_places([osm, gis])

    assert len(merged) == 1
    place = merged[0]
    assert (place.external_id, place.source) == ("osm_1", "osm")
    assert place.phone == "+7 812 555-01-01"
    assert (place.address, place.raw_website, place.socials) == (
        "Невский проспект, 22", "https://ulybka.ru", "https://vk.com/ulybka"
    )
    assert place.high_margin is True
    # Inputs are not mutated
    assert osm.raw_website is None and osm.address is None


def test_merge_prefers_real_website_over_social_link() -> None:
    """OSM often lists a VK page as 'website'; a real site from 2GIS must win, otherwise it is a false lead."""
    osm = RawPlace(
        external_id="osm_2", source="osm", name="Гараж 78", category="auto_repair",
        phone="+7 999 000-00-01", raw_website="https://vk.com/garage78",
    )
    gis = RawPlace(
        external_id="2gis_2", source="2gis", name="Гараж 78", category="auto_repair",
        phone="+79990000001", raw_website="https://garage78.ru",
    )
    place = merge_duplicate_places([osm, gis])[0]
    assert place.raw_website == "https://garage78.ru"
    assert place.socials == "https://vk.com/garage78"


def test_merge_by_name_and_proximity_without_phones() -> None:
    a = RawPlace(external_id="osm_3", source="osm", name="Кофейня Зерно", category="horeca",
                 map_url=_map_url(59.9300, 30.3100))
    near = RawPlace(external_id="2gis_3", source="2gis", name="Зерно", category="horeca",
                    map_url=_map_url(59.9305, 30.3105))
    a2 = RawPlace(external_id="osm_4", source="osm", name="Кофейня Зерно", category="horeca",
                  map_url=_map_url(59.9300, 30.3100))
    same_name_same_spot = RawPlace(external_id="2gis_4", source="2gis", name="Кофейня Зерно, кофейня",
                                   category="horeca", map_url=_map_url(59.9301, 30.3101))
    far = RawPlace(external_id="2gis_5", source="2gis", name="Кофейня Зерно", category="horeca",
                   map_url=_map_url(59.9400, 30.3100))
    other_name = RawPlace(external_id="2gis_6", source="2gis", name="Пекарня", category="horeca",
                          map_url=_map_url(59.9300, 30.3100))

    # Different normalized names ("кофейня зерно" vs "зерно") are not merged
    assert len(merge_duplicate_places([a, near])) == 2
    assert [p.external_id for p in merge_duplicate_places([a2, same_name_same_spot])] == ["osm_4"]
    # Same name ~1.1 km apart: different branches
    assert len(merge_duplicate_places([a2, far])) == 2
    # Different name at the same spot: different businesses
    assert len(merge_duplicate_places([a2, other_name])) == 2


def test_duplicate_index_matches_existing_leads() -> None:
    index = DuplicateIndex()
    index.add(0, "Улыбка", "+7 (812) 555-01-01", None)
    index.add(1, "Зерно", None, _map_url(59.93, 30.31))

    assert index.find("Совсем другое имя", "88125550101", None) == 0
    assert index.find("Зерно, кофейня", None, _map_url(59.9301, 30.3101)) == 1
    assert index.find("Зерно", None, None) is None
    assert index.find("Пекарня", "+7 999 000-00-00", _map_url(59.93, 30.31)) is None
