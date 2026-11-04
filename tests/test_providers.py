"""Unit tests for SPb map and CSV data providers."""

from pathlib import Path
import httpx
import pytest

from src.services.providers import OSMOverpassProvider, TwoGisProvider, parse_csv_leads


def test_csv_external_ids_are_stable() -> None:
    """IDs of already imported CSV leads must never change, otherwise re-imports create duplicates."""
    places = parse_csv_leads(Path("data/test_leads.csv").read_bytes())
    assert [p.external_id for p in places] == [
        "csv_ba557a73ccc5", "csv_fabaddede281", "csv_bed2b36eed14", "csv_f3ce564fe7bf",
    ]


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


def _overpass_payload(ids: range, remark: str | None = None) -> dict:
    payload: dict = {"elements": [{"type": "node", "id": i, "tags": {"name": f"Место {i}"}} for i in ids]}
    if remark is not None:
        payload["remark"] = remark
    return payload


TIMEOUT_REMARK = 'runtime error: Query timed out in "query" at line 3 after 26 seconds.'
MEMORY_REMARK = 'runtime error: Query ran out of memory in "query" at line 5.'


@pytest.mark.asyncio
@pytest.mark.parametrize("remark", [TIMEOUT_REMARK, MEMORY_REMARK])
async def test_osm_runtime_error_remark_falls_back_to_next_endpoint(remark: str, caplog) -> None:
    """Overpass reports timeouts/OOM with HTTP 200 and a 'remark'; such (partial) data must not be trusted."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        if "mail.ru" in str(request.url):
            return httpx.Response(200, json=_overpass_payload(range(1, 4), remark=remark))
        return httpx.Response(200, json=_overpass_payload(range(1, 11)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    places = await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")

    assert len(places) == 10
    assert requested == ["maps.mail.ru", "overpass-api.de"]
    assert "runtime error" in caplog.text


@pytest.mark.asyncio
async def test_osm_all_endpoints_incomplete_returns_largest_partial_result(caplog) -> None:
    """If every endpoint fails mid-query, an incomplete list is still better than nothing, with a warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "mail.ru" in str(request.url):
            return httpx.Response(200, json=_overpass_payload(range(1, 4), remark=TIMEOUT_REMARK))
        return httpx.Response(200, json=_overpass_payload(range(1, 7), remark=TIMEOUT_REMARK))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    places = await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")

    assert len(places) == 6
    assert "incomplete" in caplog.text


@pytest.mark.asyncio
async def test_osm_runtime_remark_is_not_an_error() -> None:
    """'runtime remark:' is informational; the result is complete and no fallback is needed."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        return httpx.Response(200, json=_overpass_payload(range(1, 6), remark="runtime remark: Timeout is ignored."))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    places = await OSMOverpassProvider(client=client).search_places("Центральный", "dentist_clinic")

    assert len(places) == 5
    assert requested == ["maps.mail.ru"]


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


def _twogis_page(start_id: int, count: int, total: int | None) -> dict:
    """Successful 2GIS response with `count` items starting at id `start_id`."""
    result: dict = {"items": [{"id": str(start_id + i), "name": f"Место {start_id + i}"} for i in range(count)]}
    if total is not None:
        result["total"] = total
    return {"meta": {"code": 200}, "result": result}


def _twogis_error(code: int, error_type: str, message: str) -> dict:
    return {"meta": {"code": code, "error": {"type": error_type, "message": message}}}


def _paged_client(pages: dict[int, httpx.Response], requested: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requested.append(params)
        return pages[int(params["page"])]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_twogis_fetches_all_pages_until_total() -> None:
    """Results beyond the first 50 must not be lost: pages are requested until result.total is reached."""
    requested: list[dict] = []
    pages = {
        1: httpx.Response(200, json=_twogis_page(1, 50, total=120)),
        2: httpx.Response(200, json=_twogis_page(51, 50, total=120)),
        3: httpx.Response(200, json=_twogis_page(101, 20, total=120)),
    }
    provider = TwoGisProvider(api_key="test_key", client=_paged_client(pages, requested))
    places = await provider.search_places("Центральный", "dentist_clinic")

    assert len(places) == 120
    assert [p["page"] for p in requested] == ["1", "2", "3"]
    assert all(p["page_size"] == "50" for p in requested)
    assert places[0].external_id == "2gis_1" and places[-1].external_id == "2gis_120"


@pytest.mark.asyncio
async def test_twogis_stops_on_short_page_or_not_found() -> None:
    """A page shorter than page_size, or meta.code 404 ('Results not found'), ends pagination quietly."""
    requested: list[dict] = []
    pages = {1: httpx.Response(200, json=_twogis_page(1, 7, total=None))}
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert (len(places), len(requested)) == (7, 1)

    requested.clear()
    pages = {
        1: httpx.Response(200, json=_twogis_page(1, 50, total=None)),
        2: httpx.Response(200, json=_twogis_error(404, "itemNotFound", "Results not found")),
    }
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert (len(places), len(requested)) == (50, 2)


@pytest.mark.asyncio
async def test_twogis_page_cap_limits_billable_requests(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """Every page is a paid request, so the number of pages per harvest is capped."""
    from src.services.providers import twogis

    monkeypatch.setattr(twogis, "TWOGIS_MAX_PAGES", 2)
    requested: list[dict] = []
    pages = {n: httpx.Response(200, json=_twogis_page(n * 100, 50, total=1000)) for n in range(1, 5)}
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert (len(places), len(requested)) == (100, 2)
    assert "truncated" in caplog.text


@pytest.mark.asyncio
async def test_twogis_api_error_in_meta_is_logged_without_leaking_key(caplog) -> None:
    """2GIS reports errors in meta.code/meta.error; they must be logged (not silently ignored), key masked."""
    requested: list[dict] = []
    pages = {1: httpx.Response(200, json=_twogis_error(403, "keyIsBlocked", "Key is blocked"))}
    places = await TwoGisProvider(api_key="SECRET-KEY", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert places == []
    assert "403" in caplog.text and "keyIsBlocked" in caplog.text and "Key is blocked" in caplog.text
    assert "SECRET-KEY" not in caplog.text


@pytest.mark.asyncio
async def test_twogis_error_on_later_page_keeps_collected_results(caplog) -> None:
    """A failure on page 2 must not throw away the results of page 1."""
    requested: list[dict] = []
    pages = {
        1: httpx.Response(200, json=_twogis_page(1, 50, total=120)),
        2: httpx.Response(200, json=_twogis_error(400, "paramIsOutsideSet", "Param 'page' is invalid")),
    }
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert len(places) == 50
    assert "paramIsOutsideSet" in caplog.text

    requested.clear()
    pages[2] = httpx.Response(502, text="Bad Gateway")
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    assert len(places) == 50


@pytest.mark.asyncio
async def test_twogis_deduplicates_items_repeated_across_pages() -> None:
    """Results may shift between page requests; the same id must not be returned twice."""
    requested: list[dict] = []
    pages = {
        1: httpx.Response(200, json=_twogis_page(1, 50, total=60)),
        2: httpx.Response(200, json=_twogis_page(45, 15, total=60)),  # ids 45..50 repeat
    }
    places = await TwoGisProvider(api_key="k", client=_paged_client(pages, requested)).search_places(
        "Центральный", "dentist_clinic"
    )
    ids = [p.external_id for p in places]
    assert len(ids) == len(set(ids)) == 59
