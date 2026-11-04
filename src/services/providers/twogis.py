"""2GIS Places Catalog API 3.0 provider for Saint Petersburg places."""

import logging
from typing import Any

import httpx

from src.services.checker import is_social_domain, url_host
from src.services.providers.base import (
    SPB_DISTRICTS,
    TARGET_CATEGORIES,
    BaseMapProvider,
    RawPlace,
)

logger = logging.getLogger(__name__)

TWOGIS_CATALOG_URL: str = "https://catalog.api.2gis.com/3.0/items"
# API maximum page size (docs: page_size 1..50).
TWOGIS_PAGE_SIZE: int = 50
# Each page is a separate billable request: cap pages per harvest (up to 500 places).
TWOGIS_MAX_PAGES: int = 10


class TwoGisProvider(BaseMapProvider):
    """Fetches places via 2GIS Catalog API 3.0."""

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self._client = client
        self.timeout = timeout

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Search places in SPb district using 2GIS API."""
        if not self.api_key or not self.api_key.strip():
            return []

        bbox = SPB_DISTRICTS.get(district_name)
        cat_info = TARGET_CATEGORIES.get(category_key)
        if not bbox or not cat_info:
            return []

        south, west, north, east = bbox
        query = cat_info.get("query_2gis", "")
        high_margin = bool(cat_info.get("high_margin", False))

        params: dict[str, str | int] = {
            "q": query,
            "point1": f"{west},{north}",
            "point2": f"{east},{south}",
            "fields": "items.point,items.address,items.contact_groups",
            "page_size": TWOGIS_PAGE_SIZE,
            "key": self.api_key.strip(),
        }

        if self._client is not None:
            return await self._fetch_all_pages(
                self._client, params, district_name, category_key, high_margin
            )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await self._fetch_all_pages(client, params, district_name, category_key, high_margin)

    async def _fetch_all_pages(
        self,
        client: httpx.AsyncClient,
        params: dict[str, str | int],
        district_name: str,
        category_key: str,
        high_margin: bool,
    ) -> list[RawPlace]:
        """
        Request result pages until all results are collected or TWOGIS_MAX_PAGES is reached.

        2GIS reports API errors in meta.code / meta.error; meta.code 404 ('itemNotFound')
        means there are no (more) results. A failed page keeps the results collected so far.
        """
        places: list[RawPlace] = []
        seen_ids: set[str] = set()

        for page in range(1, TWOGIS_MAX_PAGES + 1):
            try:
                response = await client.get(
                    TWOGIS_CATALOG_URL, params={**params, "page": page}, timeout=self.timeout
                )
                data = response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("2GIS request failed (page %d): %s", page, type(exc).__name__)
                break

            if not isinstance(data, dict):
                logger.warning("2GIS API returned HTTP %d (page %d)", response.status_code, page)
                break

            meta = data.get("meta") or {}
            code = meta.get("code", 200)
            if code == 404:
                break
            if code != 200:
                error = meta.get("error") or {}
                logger.warning(
                    "2GIS API error (page %d): %s %s: %s",
                    page, code, error.get("type"), error.get("message"),
                )
                break

            for place in self._parse_items(data, district_name, category_key, high_margin):
                if place.external_id not in seen_ids:
                    seen_ids.add(place.external_id)
                    places.append(place)

            result = data.get("result") or {}
            items = result.get("items") or []
            total = result.get("total")
            if len(items) < TWOGIS_PAGE_SIZE:
                break
            if isinstance(total, int) and page * TWOGIS_PAGE_SIZE >= total:
                break
        else:
            logger.warning(
                "2GIS results truncated at %d pages (%d places) for %s / %s",
                TWOGIS_MAX_PAGES, len(places), district_name, category_key,
            )

        return places

    def _parse_items(
        self,
        data: dict[str, Any],
        district_name: str,
        category_key: str,
        high_margin: bool,
    ) -> list[RawPlace]:
        """Safely parse 2GIS catalog items into RawPlace list."""
        places: list[RawPlace] = []
        result = data.get("result") or {}
        items = result.get("items") or []

        for item in items:
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

            phones, websites, socials_list = self._extract_contacts(item)
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
                )
            )

        return places

    def _extract_contacts(
        self, item: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        """Extract phones, websites, and socials from contact_groups."""
        phones: list[str] = []
        websites: list[str] = []
        socials: list[str] = []

        for group in item.get("contact_groups") or []:
            for contact in group.get("contacts") or []:
                c_type = str(contact.get("type") or "").lower().strip()
                val = contact.get("value") or contact.get("text") or contact.get("url")
                if not val or not str(val).strip():
                    continue
                val_str = str(val).strip()

                if c_type == "phone":
                    if val_str not in phones:
                        phones.append(val_str)
                elif c_type in ("website", "url"):
                    target = socials if is_social_domain(url_host(val_str)) else websites
                    if val_str not in target:
                        target.append(val_str)
                elif (
                    c_type in ("vkontakte", "vk", "telegram", "instagram", "whatsapp", "social")
                    and val_str not in socials
                ):
                    socials.append(val_str)

        return phones, websites, socials
