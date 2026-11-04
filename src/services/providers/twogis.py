"""2GIS Places Catalog API 3.0 provider for Saint Petersburg places."""

import logging
from typing import Any
import httpx

from src.services.checker import is_social_domain
from src.services.providers.base import (
    SPB_DISTRICTS,
    TARGET_CATEGORIES,
    BaseMapProvider,
    RawPlace,
)

logger = logging.getLogger(__name__)

TWOGIS_CATALOG_URL: str = "https://catalog.api.2gis.com/3.0/items"


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

        params = {
            "q": query,
            "point1": f"{west},{north}",
            "point2": f"{east},{south}",
            "fields": "items.point,items.address,items.contact_groups",
            "page_size": 50,
            "key": self.api_key.strip(),
        }

        try:
            if self._client is not None:
                response = await self._client.get(
                    TWOGIS_CATALOG_URL, params=params, timeout=self.timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(TWOGIS_CATALOG_URL, params=params)

            if response.status_code != 200:
                logger.warning("2GIS API returned status %d", response.status_code)
                return []

            data = response.json()
            return self._parse_items(data, district_name, category_key, high_margin)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("2GIS request failed: %s", exc)
            return []

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

            if not socials_str and raw_website:
                host = None
                try:
                    url_to_parse = (
                        raw_website if raw_website.startswith(("http://", "https://"))
                        else f"https://{raw_website}"
                    )
                    host = httpx.URL(url_to_parse).host
                except Exception:
                    pass
                if is_social_domain(host):
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
                    host = None
                    try:
                        url_parsed = (
                            val_str if val_str.startswith(("http://", "https://"))
                            else f"https://{val_str}"
                        )
                        host = httpx.URL(url_parsed).host
                    except Exception:
                        pass
                    if is_social_domain(host):
                        if val_str not in socials:
                            socials.append(val_str)
                    else:
                        if val_str not in websites:
                            websites.append(val_str)
                elif c_type in ("vkontakte", "vk", "telegram", "instagram", "whatsapp", "social"):
                    if val_str not in socials:
                        socials.append(val_str)

        return phones, websites, socials
