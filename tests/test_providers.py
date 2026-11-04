"""Unit tests for SPb map and CSV data providers."""

from pathlib import Path
import httpx
import pytest

from src.services.providers import OSMOverpassProvider, TwoGisProvider, parse_csv_leads


def test_parse_real_csv_leads() -> None:
    """Verify parsing real test_leads.csv yields exactly 4 expected RawPlaces."""
    places = parse_csv_leads(Path("data/test_leads.csv").read_bytes())
    assert len(places) == 4

    p1, p2, p3, p4 = places
    assert p1.name == "Стоматология Улыбка СПб"
    assert (p1.category, p1.district, p1.address) == ("Стоматология", "Петроградский", "Большой пр. П.С., 44")
    assert (p1.phone, p1.raw_website, p1.socials) == (
        "+7 (812) 555-01-01", "https://broken-domain-dentist-spb-404.ru", "https://vk.com/dent_spb_test"
    )
    assert (p1.source, p1.high_margin) == ("csv", True)
    assert p1.external_id.startswith("csv_")

    assert (p2.name, p2.category, p2.district) == ("Детейлинг Гараж 78", "Автосервис", "Приморский")
    assert p2.raw_website == p2.socials == "https://vk.com/detailing78_test"
    assert p2.high_margin is True

    assert (p3.name, p3.raw_website, p3.socials, p3.high_margin) == (
        "Студия мебели ЛофтНева", None, "https://t.me/loftneva_test", True
    )

    assert (p4.name, p4.category, p4.raw_website, p4.socials, p4.high_margin) == (
        "Кофейня с рабочим сайтом", "Кафе", "https://ya.ru", None, False
    )


def test_parse_csv_cp1251_and_comma() -> None:
    """Verify cp1251 decoding and comma delimiter support."""
    content = "имя,категория,адрес,телефон,сайт\nТест Клиника,Клиника,Невский 1,+79990001122,https://vk.com/c"
    places = parse_csv_leads(content.encode("cp1251"))
    assert len(places) == 1
    assert (places[0].name, places[0].phone, places[0].socials, places[0].high_margin) == (
        "Тест Клиника", "+79990001122", "https://vk.com/c", True
    )


def test_parse_csv_empty_returns_empty() -> None:
    """Empty CSV bytes should return empty list."""
    assert parse_csv_leads(b"") == []
    assert parse_csv_leads(b"\n\n   \n") == []


@pytest.mark.asyncio
async def test_osm_overpass_mock_parsing() -> None:
    """Verify OSM Overpass response parsing for nodes, ways, and coordinate URLs."""
    mock_payload = {
        "elements": [
            {
                "type": "node", "id": 101, "lat": 59.93, "lon": 30.31,
                "tags": {
                    "name": "Дент Про", "phone": "+7 812 111-22-33", "website": "https://dentpro.ru",
                    "addr:street": "Невский пр.", "addr:housenumber": "22",
                },
            },
            {
                "type": "way", "id": 202, "center": {"lat": 59.94, "lon": 30.32},
                "tags": {
                    "name": "Клиника Смайл", "contact:phone": "+7 812 333-44-55",
                    "contact:vk": "https://vk.com/smile_spb",
                },
            },
            {"type": "node", "id": 303, "tags": {"amenity": "dentist"}},
            {
                "type": "node", "id": 404, "lat": 59.95, "lon": 30.33,
                "tags": {"name": "ВК Стоматология", "website": "https://vk.com/dent_vk"},
            },
        ]
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=mock_payload)))
    places = await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")

    assert len(places) == 3
    assert (places[0].external_id, places[0].name, places[0].address) == ("osm_101", "Дент Про", "Невский пр., 22")
    assert places[0].map_url == "https://yandex.ru/maps/?pt=30.31,59.93&z=17&l=map"
    assert places[0].high_margin is True

    assert (places[1].external_id, places[1].name, places[1].socials) == ("osm_202", "Клиника Смайл", "https://vk.com/smile_spb")
    assert places[1].map_url == "https://yandex.ru/maps/?pt=30.32,59.94&z=17&l=map"

    assert (places[2].external_id, places[2].socials) == ("osm_404", "https://vk.com/dent_vk")


@pytest.mark.asyncio
async def test_osm_multiple_websites_separated_by_semicolon() -> None:
    """OSM multi-value tags use ';': the joined string must not be checked as one (invalid) URL."""

    def node(elem_id: int, website: str, **extra_tags: str) -> dict:
        return {"type": "node", "id": elem_id, "tags": {"name": f"Место {elem_id}", "website": website, **extra_tags}}

    mock_payload = {
        "elements": [
            node(1, "https://first-site.ru;https://second-site.ru"),
            node(2, "https://vk.com/clinic; https://real-clinic.ru"),
            node(3, "https://vk.com/clinic;https://t.me/clinic"),
            node(4, " ; "),
            node(5, "https://vk.com/from_website;https://site5.ru", **{"contact:vk": "https://vk.com/contact_vk"}),
        ]
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=mock_payload)))
    places = {
        p.external_id: p
        for p in await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")
    }

    # First real website wins
    assert (places["osm_1"].raw_website, places["osm_1"].socials) == ("https://first-site.ru", None)
    # A social link listed first must not hide the real website; it is kept as a social contact
    assert (places["osm_2"].raw_website, places["osm_2"].socials) == (
        "https://real-clinic.ru", "https://vk.com/clinic"
    )
    # Only social links: the first one is checked (-> social_only), all are kept as socials
    assert (places["osm_3"].raw_website, places["osm_3"].socials) == (
        "https://vk.com/clinic", "https://vk.com/clinic, https://t.me/clinic"
    )
    # Separators only -> no website at all
    assert (places["osm_4"].raw_website, places["osm_4"].socials) == (None, None)
    # Explicit contact:* socials take precedence, as for single-value tags
    assert (places["osm_5"].raw_website, places["osm_5"].socials) == (
        "https://site5.ru", "https://vk.com/contact_vk"
    )


@pytest.mark.asyncio
async def test_osm_overpass_fallback_and_failure() -> None:
    """Verify fallback from first failing endpoint to second, and handling when all fail."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if "mail.ru" in str(request.url):
            return httpx.Response(504, text="Gateway Timeout")
        return httpx.Response(200, json={"elements": [{"id": 1, "tags": {"name": "ОК"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    places = await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")
    assert len(places) == 1 and attempts == 2

    fail_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="Error")))
    assert await OSMOverpassProvider(client=fail_client).search_places("Центральный", "dentist_clinic") == []


@pytest.mark.asyncio
async def test_twogis_provider() -> None:
    """Verify 2GIS provider empty key behavior, parsing items, and error handling."""
    assert await TwoGisProvider(api_key=None).search_places("Центральный", "auto_repair") == []

    mock_2gis_resp = {
        "result": {
            "items": [
                {
                    "id": "70000001",
                    "name": "АвтоСервис 24",
                    "address_name": "ул. Мира, 10",
                    "point": {"lat": 59.96, "lon": 30.31},
                    "contact_groups": [
                        {
                            "contacts": [
                                {"type": "phone", "value": "+7 812 777-88-99"},
                                {"type": "website", "value": "https://autoservice24.ru"},
                                {"type": "vkontakte", "value": "https://vk.com/as24"},
                            ]
                        }
                    ],
                }
            ]
        }
    }

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=mock_2gis_resp)))
    places = await TwoGisProvider(api_key="test_key", client=client).search_places("Петроградский", "auto_repair")

    assert len(places) == 1
    p = places[0]
    assert (p.external_id, p.source, p.name, p.district) == ("2gis_70000001", "2gis", "АвтоСервис 24", "Петроградский")
    assert (p.address, p.phone, p.raw_website, p.socials) == (
        "ул. Мира, 10", "+7 812 777-88-99", "https://autoservice24.ru", "https://vk.com/as24"
    )
    assert p.map_url == "https://yandex.ru/maps/?pt=30.31,59.96&z=17&l=map"
    assert p.high_margin is True

    err_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="Error")))
    assert await TwoGisProvider(api_key="test_key", client=err_client).search_places("Петроградский", "auto_repair") == []
