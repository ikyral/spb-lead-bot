"""2GIS Places Catalog API 3.0 provider for Saint Petersburg places."""

import logging
from typing import Any

import httpx

from src.services.api_client import (
    ApiClient,
    ProviderAuthError,
    ProviderError,
    RateLimiter,
)
from src.services.checker import is_social_domain, url_host
from src.services.providers.base import (
    SPB_DISTRICTS,
    TARGET_CATEGORIES,
    BaseMapProvider,
    RawPlace,
    SearchArea,
    SearchPage,
    SearchQuery,
)

logger = logging.getLogger(__name__)

TWOGIS_CATALOG_URL: str = "https://catalog.api.2gis.com/3.0/items"
TWOGIS_FIRM_URL: str = "https://2gis.ru/firm/{firm_id}"
# API maximum page size (docs: page_size 1..50).
TWOGIS_PAGE_SIZE: int = 50
# Each page is a separate billable request: cap pages per harvest (up to 500 places).
TWOGIS_MAX_PAGES: int = 10
# Shared by all provider instances, so manual searches and collection do not add up.
_RATE_LIMITER = RateLimiter(requests_per_second=2.0)
# With this many items and no contacts on any of them, the key has no access to contacts.
_MIN_ITEMS_TO_DETECT_MISSING_CONTACTS: int = 5
_NO_CONTACTS_WARNING: str = (
    "2ГИС вернул организации без контактов: похоже, у ключа нет доступа к items.contact_groups "
    "(демо-ключи его не имеют, для остальных он подключается за доплату через отдел продаж 2ГИС). "
    "Без телефона и сайта организации не проходят скоринг, новые лиды появляться не будут."
)


def _inspect_body(payload: dict[str, Any]) -> tuple[int | None, str]:
    """2GIS reports API errors in meta.code / meta.error, sometimes with HTTP 200."""
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None, ""
    code = meta.get("code")
    error = meta.get("error")
    details = ""
    if isinstance(error, dict):
        details = f"{error.get('type')}: {error.get('message')}"
    return (code if isinstance(code, int) else None), details


class TwoGisProvider(BaseMapProvider):
    """Fetches places via 2GIS Catalog API 3.0."""

    name = "2gis"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self._api = ApiClient(
            "2ГИС", client=client, limiter=_RATE_LIMITER, timeout=timeout, secrets=[(api_key or "").strip()]
        )

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Search places in SPb district using 2GIS API."""
        if not self.api_key or not self.api_key.strip():
            return []

        bbox = SPB_DISTRICTS.get(district_name)
        cat_info = TARGET_CATEGORIES.get(category_key)
        if not bbox or not cat_info:
            return []

        query = SearchQuery(
            text=cat_info.get("query_2gis", ""),
            category=category_key,
            high_margin=bool(cat_info.get("high_margin", False)),
        )
        return await self._fetch_all_pages(query, SearchArea(bbox=bbox, district=district_name))

    async def search(
        self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = TWOGIS_PAGE_SIZE
    ) -> SearchPage:
        """Request one page of results (one billable request). API errors raise ProviderError."""
        if not self.api_key or not self.api_key.strip():
            raise ProviderAuthError("2ГИС: не задан TWOGIS_API_KEY")
        return await self._fetch_page(query, area, page, page_size)

    async def _fetch_page(
        self,
        query: SearchQuery,
        area: SearchArea,
        page: int,
        page_size: int,
        max_attempts: int | None = None,
    ) -> SearchPage:
        south, west, north, east = area.bbox
        response = await self._api.request_json(
            "GET",
            TWOGIS_CATALOG_URL,
            params={
                "q": query.text,
                "point1": f"{west},{north}",
                "point2": f"{east},{south}",
                "fields": "items.point,items.address,items.contact_groups",
                "page_size": page_size,
                "page": page,
                "key": (self.api_key or "").strip(),
            },
            inspect_body=_inspect_body,
            # meta.code 404 ('itemNotFound') means there are no (more) results.
            ok_statuses=frozenset({200, 404}),
            max_attempts=max_attempts,
        )
        if response.status == 404:
            return SearchPage(places=[])

        result = response.data.get("result")
        result = result if isinstance(result, dict) else {}
        items = result.get("items")
        items = items if isinstance(items, list) else []
        total = result.get("total")
        has_more = len(items) >= page_size and not (isinstance(total, int) and page * page_size >= total)

        no_contacts = len(items) >= _MIN_ITEMS_TO_DETECT_MISSING_CONTACTS and not any(
            isinstance(item, dict) and item.get("contact_groups") for item in items
        )
        return SearchPage(
            places=self._parse_items(response.data, area.district, query.category, query.high_margin),
            has_more=has_more,
            warning=_NO_CONTACTS_WARNING if no_contacts else None,
        )

    async def _fetch_all_pages(self, query: SearchQuery, area: SearchArea) -> list[RawPlace]:
        """
        Request result pages until all results are collected or TWOGIS_MAX_PAGES is reached.

        A failed page keeps the results collected so far. No retries here: the admin is
        waiting for the answer, and the search can simply be repeated.
        """
        places: list[RawPlace] = []
        seen_ids: set[str] = set()

        for page in range(1, TWOGIS_MAX_PAGES + 1):
            try:
                result = await self._fetch_page(query, area, page, TWOGIS_PAGE_SIZE, max_attempts=1)
            except ProviderError as exc:
                logger.warning("2GIS request failed (page %d): %s", page, exc)
                break

            for place in result.places:
                if place.external_id not in seen_ids:
                    seen_ids.add(place.external_id)
                    places.append(place)

            if not result.has_more:
                break
        else:
            logger.warning(
                "2GIS results truncated at %d pages (%d places) for %s / %s",
                TWOGIS_MAX_PAGES, len(places), area.district, query.category,
            )

        return places

    def _parse_items(
        self,
        data: dict[str, Any],
        district_name: str | None,
        category_key: str,
        high_margin: bool,
    ) -> list[RawPlace]:
        """Safely parse 2GIS catalog items into RawPlace list."""
        places: list[RawPlace] = []
        result = data.get("result")
        items = result.get("items") if isinstance(result, dict) else None

        for item in items if isinstance(items, list) else []:
            # A text search may also return buildings and streets: only organizations are leads.
            if not isinstance(item, dict) or item.get("type") not in (None, "branch"):
                continue
            raw_id = item.get("id")
            raw_name = item.get("name")
            if not raw_id or not raw_name or not str(raw_name).strip():
                continue

            external_id = f"2gis_{raw_id}"
            name = str(raw_name).strip()

            address = None
            addr_data = item.get("address")
            if isinstance(addr_data, dict):
                address = addr_data.get("name")
            elif isinstance(addr_data, str):
                address = addr_data
            if not address:
                address = item.get("address_name")
            address_str = str(address).strip() if address and str(address).strip() else None

            map_url = None
            point = item.get("point")
            if isinstance(point, dict):
                lat = point.get("lat")
                lon = point.get("lon")
                if lat is not None and lon is not None:
                    map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"

            phones, websites, socials_list, emails = self._extract_contacts(item)
            phone_str = ", ".join(phones) if phones else None
            raw_website = websites[0] if websites else None
            socials_str = ", ".join(socials_list) if socials_list else None

            if not socials_str and raw_website and is_social_domain(url_host(raw_website)):
                socials_str = raw_website

            places.append(
                RawPlace(
                    external_id=external_id,
                    source="2gis",
                    name=name,
                    category=category_key,
                    district=district_name,
                    address=address_str,
                    phone=phone_str,
                    socials=socials_str,
                    raw_website=raw_website,
                    map_url=map_url,
                    high_margin=high_margin,
                    email=emails[0] if emails else None,
                    # Ids may carry a hash suffix ('70000001_ab12'), the card URL uses the numeric part.
                    source_url=TWOGIS_FIRM_URL.format(firm_id=str(raw_id).split("_", 1)[0]),
                )
            )

        return places

    def _extract_contacts(
        self, item: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Extract phones, websites, socials and emails from contact_groups."""
        phones: list[str] = []
        websites: list[str] = []
        socials: list[str] = []
        emails: list[str] = []

        groups = item.get("contact_groups")
        for group in groups if isinstance(groups, list) else []:
            contacts = group.get("contacts") if isinstance(group, dict) else None
            for contact in contacts if isinstance(contacts, list) else []:
                if not isinstance(contact, dict):
                    continue
                c_type = str(contact.get("type") or "").lower().strip()
                val = contact.get("value") or contact.get("text") or contact.get("url")
                if not val or not str(val).strip():
                    continue
                val_str = str(val).strip()

                if c_type == "phone":
                    if val_str not in phones:
                        phones.append(val_str)
                elif c_type == "email":
                    if val_str not in emails:
                        emails.append(val_str)
                elif c_type in ("website", "url"):
                    target = socials if is_social_domain(url_host(val_str)) else websites
                    if val_str not in target:
                        target.append(val_str)
                elif (
                    c_type in ("vkontakte", "vk", "telegram", "instagram", "whatsapp", "social")
                    and val_str not in socials
                ):
                    socials.append(val_str)

        return phones, websites, socials, emails
