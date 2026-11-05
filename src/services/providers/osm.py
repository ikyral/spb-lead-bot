"""OpenStreetMap Overpass API provider for Saint Petersburg places."""

import logging
from collections.abc import Iterable
from typing import Any

import httpx

from src.services.api_client import ApiClient, ProviderError, ProviderTemporaryError, RateLimiter
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

DEFAULT_OVERPASS_ENDPOINTS: list[str] = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OSM_ELEMENT_URL: str = "https://www.openstreetmap.org/{element_type}/{element_id}"
# Public Overpass servers ask for moderate use; shared by all provider instances.
_RATE_LIMITER = RateLimiter(requests_per_second=1.0)


class OSMOverpassProvider(BaseMapProvider):
    """Fetches places via OSM Overpass API with server fallback."""

    name = "osm"

    def __init__(
        self,
        endpoints: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoints = endpoints or list(DEFAULT_OVERPASS_ENDPOINTS)
        self._api = ApiClient("Overpass", client=client, limiter=_RATE_LIMITER, timeout=timeout)

    def build_query(
        self, bbox: tuple[float, float, float, float], osm_tags: Iterable[tuple[str, str]]
    ) -> str:
        """Construct Overpass QL query for given bounding box and OSM tags."""
        south, west, north, east = bbox
        lines = ["[out:json][timeout:25];", "("]
        for key, val in osm_tags:
            lines.append(f'  node["{key}"="{val}"]({south},{west},{north},{east});')
            lines.append(f'  way["{key}"="{val}"]({south},{west},{north},{east});')
        lines.append(");")
        lines.append("out center tags;")
        return "\n".join(lines)

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Search places in SPb district for a target category."""
        bbox = SPB_DISTRICTS.get(district_name)
        cat_info = TARGET_CATEGORIES.get(category_key)
        if not bbox or not cat_info:
            return []

        osm_tags: list[tuple[str, str]] = cat_info.get("osm_tags", [])
        if not osm_tags:
            return []

        query = SearchQuery(
            text="",
            category=category_key,
            high_margin=bool(cat_info.get("high_margin", False)),
            osm_tags=tuple(osm_tags),
        )
        places, _complete = await self._query_endpoints(query, SearchArea(bbox=bbox, district=district_name))
        return places

    async def search(
        self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50
    ) -> SearchPage:
        """
        Overpass has no pages: everything comes at once, so only page 1 exists.

        Free-text queries have no OSM tags and are skipped. If all servers fail and
        nothing was received, ProviderTemporaryError is raised.
        """
        if page > 1 or not query.osm_tags:
            return SearchPage(places=[])
        places, complete = await self._query_endpoints(query, area)
        if not complete and not places:
            raise ProviderTemporaryError("Overpass: все серверы недоступны")
        return SearchPage(places=places)

    async def _query_endpoints(self, search: SearchQuery, area: SearchArea) -> tuple[list[RawPlace], bool]:
        """Try the servers in turn. Returns the places and whether the result is complete."""
        query = self.build_query(area.bbox, search.osm_tags)
        district_name, category_key, high_margin = area.district, search.category, search.high_margin

        # Best incomplete result, used only if no endpoint returns a complete one.
        partial: list[RawPlace] = []

        for endpoint in self.endpoints:
            try:
                # The next server is the retry here.
                response = await self._api.request_json("POST", endpoint, data={"data": query}, max_attempts=1)
                data = response.data
                places = self._parse_elements(data, district_name, category_key, high_margin)

                # Timeouts and memory exhaustion come with HTTP 200 and a 'remark' starting with
                # 'runtime error:'; elements may then be missing. 'runtime remark:' is informational.
                remark = str(data.get("remark") or "")
                if remark.startswith("runtime error"):
                    logger.warning(
                        "Overpass endpoint %s returned an incomplete result (%d places): %s",
                        endpoint, len(places), remark,
                    )
                    if len(places) > len(partial):
                        partial = places
                    continue

                return places, True
            except (ProviderError, ValueError, KeyError, AttributeError) as exc:
                logger.warning("Overpass endpoint %s failed: %s", endpoint, exc)

        if partial:
            logger.warning(
                "All Overpass endpoints failed to complete the query for %s / %s; "
                "using an incomplete result with %d places",
                district_name, category_key, len(partial),
            )
        return partial, False

    def _parse_elements(
        self,
        data: dict[str, Any],
        district_name: str | None,
        category_key: str,
        high_margin: bool,
    ) -> list[RawPlace]:
        """Parse raw Overpass JSON elements into RawPlace objects."""
        places: list[RawPlace] = []
        seen_ids: set[str] = set()

        for elem in data.get("elements", []):
            tags = elem.get("tags") or {}
            raw_name = tags.get("name")
            if not raw_name or not str(raw_name).strip():
                continue
            name = str(raw_name).strip()

            elem_id = elem.get("id")
            if elem_id is None:
                continue
            external_id = f"osm_{elem_id}"
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)

            phone = tags.get("phone") or tags.get("contact:phone")
            phone_str = str(phone).strip() if phone and str(phone).strip() else None

            raw_site = tags.get("website") or tags.get("contact:website") or tags.get("url")
            # OSM separates multiple values with ';'. Prefer the first real website,
            # so a social link listed first does not hide it.
            site_candidates = [
                part.strip() for part in str(raw_site or "").split(";") if part.strip()
            ]
            real_sites = [u for u in site_candidates if not is_social_domain(url_host(u))]
            social_sites = [u for u in site_candidates if is_social_domain(url_host(u))]
            if real_sites:
                raw_website = real_sites[0]
            elif social_sites:
                raw_website = social_sites[0]
            else:
                raw_website = None

            social_parts: list[str] = []
            for s_key in ("contact:vk", "contact:telegram", "contact:instagram", "vk", "telegram"):
                val = tags.get(s_key)
                if val and str(val).strip():
                    v_str = str(val).strip()
                    if v_str not in social_parts:
                        social_parts.append(v_str)

            socials = ", ".join(social_parts) if social_parts else None
            if not socials and social_sites:
                socials = ", ".join(social_sites)

            street = tags.get("addr:street")
            house = tags.get("addr:housenumber")
            addr_parts = [str(p).strip() for p in (street, house) if p and str(p).strip()]
            address = ", ".join(addr_parts) if addr_parts else None

            lat = elem.get("lat")
            lon = elem.get("lon")
            if (lat is None or lon is None) and isinstance(elem.get("center"), dict):
                lat = elem["center"].get("lat")
                lon = elem["center"].get("lon")

            map_url = None
            if lat is not None and lon is not None:
                map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"

            places.append(
                RawPlace(
                    external_id=external_id,
                    source="osm",
                    name=name,
                    category=category_key,
                    district=district_name,
                    address=address,
                    phone=phone_str,
                    socials=socials,
                    raw_website=raw_website,
                    map_url=map_url,
                    high_margin=high_margin,
                    source_url=(
                        OSM_ELEMENT_URL.format(element_type=elem["type"], element_id=elem_id)
                        if elem.get("type") in ("node", "way", "relation")
                        else None
                    ),
                )
            )

        return places
