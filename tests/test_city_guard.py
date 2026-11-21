"""Cards and search pages of other cities: the guard, the counter, the summary line and the redirect log."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.bot.formatters import format_collection_report
from src.services.city_guard import foreign_card_reason, foreign_page_reasons, point_outside_spb
from src.services.collector import CollectResult
from src.services.providers.base import SearchArea, SearchQuery
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider

QUERY = SearchQuery(text="шаурма", category="street_food")
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Выборгский район")


def _item(firm_id: str, lat: float | None = None, lon: float | None = None, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": firm_id,
        "name": f"Фирма {firm_id}",
        "type": "branch",
        "contact_groups": [{"contacts": [{"type": "phone", "value": "+7 812 555-00-01"}]}],
        **extra,
    }
    if lat is not None and lon is not None:
        item["point"] = {"lat": lat, "lon": lon}
    return item


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(59.93, 30.33), (60.20, 29.69), (60.00, 29.77), (59.90, 30.30)],
)
def test_spb_points_are_kept(lat: float, lon: float) -> None:
    assert foreign_card_reason(_item("1", lat, lon)) is None


def test_card_without_point_is_kept() -> None:
    assert foreign_card_reason(_item("1")) is None
    assert point_outside_spb(None, None) is False
    assert point_outside_spb("x", 30.0) is False


@pytest.mark.parametrize(("lat", "lon"), [(55.60, 37.36), (60.70, 28.75)])
def test_moscow_region_and_vyborg_points_are_rejected(lat: float, lon: float) -> None:
    reason = foreign_card_reason(_item("1", lat, lon))
    assert reason is not None
    assert "outside" in reason


@pytest.mark.parametrize("city", ["Москва", "Выборг", "Московский"])
def test_clearly_foreign_card_city_is_rejected(city: str) -> None:
    reason = foreign_card_reason(_item("1", adm_div=[{"type": "city", "name": city}]))
    assert reason is not None
    assert city in reason


@pytest.mark.parametrize("city", ["Санкт-Петербург", "Зеленогорск", "Кронштадт"])
def test_spb_card_city_is_kept(city: str) -> None:
    assert foreign_card_reason(_item("1", adm_div=[{"type": "city", "name": city}])) is None


def test_unclear_adm_div_is_ignored() -> None:
    assert foreign_card_reason(_item("1", adm_div="oops")) is None
    assert foreign_card_reason(_item("1", adm_div=[{"type": "district", "name": "Москва"}, 5])) is None


def test_places_drops_foreign_cards_counts_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    provider = TwoGisCdpProvider(CdpOptions())
    items = [_item("1", 59.93, 30.33), _item("2", 55.60, 37.36), _item("3", 60.70, 28.75), _item("4", 60.20, 29.69)]
    with caplog.at_level(logging.WARNING):
        places = provider._places([{"result": {"items": items}}], QUERY, AREA)
    assert [place.external_id for place in places] == ["2gis_1", "2gis_4"]
    assert provider.foreign_city_dropped == 2
    assert "Фирма 2" in caplog.text
    assert "55.6" in caplog.text
    assert "+7 812" not in caplog.text


def _report(result: CollectResult) -> str:
    report = MagicMock(status="ok", result=result, duration_seconds=1.0, pool_size=0)
    return format_collection_report(report)


def test_summary_line_only_when_something_was_dropped() -> None:
    assert "не из СПб" not in _report(CollectResult(found=1))
    text = _report(CollectResult(found=1, foreign_city_dropped=3))
    assert "• Отсеяно не из СПб: <b>3</b>" in text


def test_redirect_to_another_city_warns() -> None:
    url = "https://2gis.ru/pikalevo/search/шаурма Выборгский район?m=31.2,60.3/7"
    reasons = foreign_page_reasons(url, "шаурма Выборгский район — 2ГИС")
    assert any("title" in reason for reason in reasons)
    assert any("map center" in reason for reason in reasons)


def test_normal_district_page_does_not_warn() -> None:
    url = "https://2gis.ru/vsevolozhsk/search/шаурма Калининский район Санкт-Петербург?m=30.607855,60.004589/10"
    title = "Шаурма калининский район в Санкт-Петербурге на карте: ☎ телефоны, ★ отзывы — 2ГИС"
    assert foreign_page_reasons(url, title) == []


def test_title_without_city_alone_warns_by_title_rule() -> None:
    reasons = foreign_page_reasons("https://2gis.ru/spb/search/x?m=30.3,59.9/11", "x — 2ГИС")
    assert len(reasons) == 1
    assert "title" in reasons[0]
