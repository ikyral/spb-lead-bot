"""2GIS Places Catalog API 3.0 provider for Saint Petersburg places."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import Settings
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
from src.services.twogis_links import DEFAULT_CITY_SLUG, build_card_url

logger = logging.getLogger(__name__)

TWOGIS_CATALOG_URL: str = "https://catalog.api.2gis.com/3.0/items"
# API maximum page size (docs: page_size 1..50).
TWOGIS_PAGE_SIZE: int = 50
# Each page is a separate billable request: cap pages per harvest (up to 500 places).
TWOGIS_MAX_PAGES: int = 10
# A demo key returns at most this much; a larger page_size is answered with an error.
DEMO_PAGE_SIZE: int = 10
DEMO_MAX_PAGES: int = 5
_BASE_FIELDS: str = "items.point,items.address,items.rubrics"
_CONTACT_FIELD: str = "items.contact_groups"
# A key over its limit may be blocked until the next month: 429 is not worth repeating.
_NO_RETRY_STATUSES: frozenset[int] = frozenset({429})
# Shared by all provider instances, so manual searches and collection do not add up.
_RATE_LIMITER = RateLimiter(requests_per_second=2.0)
# With this many items and no contacts on any of them, the key has no access to contacts.
_MIN_ITEMS_TO_DETECT_MISSING_CONTACTS: int = 5
_NO_CONTACTS_WARNING: str = (
    "2ГИС вернул организации без контактов, а LEADS_REQUIRE_CONTACT=true требует их. Нужен доступ к "
    "items.contact_groups (демо-ключи его не имеют, для остальных он подключается за доплату через отдел "
    "продаж 2ГИС) и DGIS_REQUEST_CONTACTS=true. Иначе задайте LEADS_REQUIRE_CONTACT=false: тогда лиды "
    "выдаются со ссылкой на карточку."
)
_AUTH_FAILURE_HINT: str = (
    "2ГИС не принял ключ: срок демо-ключа мог закончиться, ключ мог быть заблокирован"
)
_AUTH_FAILURE_CONTACTS_HINT: str = ", либо у него нет доступа к контактам (проверьте DGIS_REQUEST_CONTACTS)"


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


@dataclass(frozen=True)
class TwoGisOptions:
    """How the API is used. Defaults suit a demo key without access to contacts."""

    city_slug: str = DEFAULT_CITY_SLUG
    demo_key: bool = True
    request_contacts: bool = False
    require_contacts: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "TwoGisOptions":
        return cls(
            city_slug=settings.SCRAPER_CITY_SLUG,
            demo_key=settings.DGIS_DEMO_KEY,
            request_contacts=settings.DGIS_REQUEST_CONTACTS,
            require_contacts=settings.LEADS_REQUIRE_CONTACT,
        )


class TwoGisProvider(BaseMapProvider):
    """Fetches places via 2GIS Catalog API 3.0."""

    name = "2gis"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        options: TwoGisOptions | None = None,
        spend_request: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.api_key = api_key
        self._options = options or TwoGisOptions()
        self._max_page_size = DEMO_PAGE_SIZE if self._options.demo_key else TWOGIS_PAGE_SIZE
        self._max_pages = DEMO_MAX_PAGES if self._options.demo_key else TWOGIS_MAX_PAGES
        self.progress_scope = "demo" if self._options.demo_key else "full"
        self.auth_failure_hint = _AUTH_FAILURE_HINT + (
            _AUTH_FAILURE_CONTACTS_HINT if self._options.request_contacts else ""
        )
        self._api = ApiClient(
            "2ГИС",
            client=client,
            limiter=_RATE_LIMITER,
            timeout=timeout,
            secrets=[(api_key or "").strip()],
            no_retry_statuses=_NO_RETRY_STATUSES,
            before_request=spend_request,
        )
        # Reported once per run: the provider lives as long as the bot, close() ends a run.
        self._missing_contacts_noted = False
        self._page_size_noted = False

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

    async def close(self) -> None:
        """The end of a run: what is reported once per run may be reported again."""
        self._missing_contacts_noted = False
        self._page_size_noted = False

    async def search(
        self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = TWOGIS_PAGE_SIZE
    ) -> SearchPage:
        """Request one page of results (one billable request). API errors raise ProviderError."""
        if not self.api_key or not self.api_key.strip():
            raise ProviderAuthError("2ГИС: не задан TWOGIS_API_KEY")
        # Not a request the API would answer: the demo key has no pages further than this.
        if self._options.demo_key and page > DEMO_MAX_PAGES:
            return SearchPage(places=[])
        return await self._fetch_page(query, area, page, self._allowed_page_size(page_size))

    def _allowed_page_size(self, page_size: int) -> int:
        if page_size > self._max_page_size and not self._page_size_noted:
            self._page_size_noted = True
            logger.info(
                "2GIS: page size %d lowered to %d, the most this key allows", page_size, self._max_page_size
            )
        return min(page_size, self._max_page_size)

    def _note_missing_contacts(self) -> str | None:
        """
        An expected state rather than an error, unless contacts are required: then it is an alert.

        Returns the alert; said once per run either way.
        """
        if self._missing_contacts_noted:
            return None
        self._missing_contacts_noted = True
        if self._options.require_contacts:
            return _NO_CONTACTS_WARNING
        if self._options.request_contacts:
            logger.warning("2GIS returned organizations without contacts: the key seems to have no access to them")
        else:
            logger.info("2GIS contacts are not requested (DGIS_REQUEST_CONTACTS=false): leads link to their cards")
        return None

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
                "fields": f"{_BASE_FIELDS},{_CONTACT_FIELD}" if self._options.request_contacts else _BASE_FIELDS,
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
        has_more = (
            len(items) >= page_size
            and not (isinstance(total, int) and page * page_size >= total)
            and not (self._options.demo_key and page >= DEMO_MAX_PAGES)
        )

        any_contacts = any(isinstance(item, dict) and item.get("contact_groups") for item in items)
        # When contacts are not requested, there is no need to wait for enough items to tell.
        enough_to_tell = len(items) >= _MIN_ITEMS_TO_DETECT_MISSING_CONTACTS or (
            items and not self._options.request_contacts
        )
        return SearchPage(
            places=parse_catalog_items(
                response.data, area.district, query.category, query.high_margin, self._options.city_slug
            ),
            has_more=has_more,
            warning=self._note_missing_contacts() if enough_to_tell and not any_contacts else None,
        )

    async def _fetch_all_pages(self, query: SearchQuery, area: SearchArea) -> list[RawPlace]:
        """
        Request result pages until all results are collected or the limit of pages is reached.

        A failed page keeps the results collected so far. No retries here: the admin is
        waiting for the answer, and the search can simply be repeated.
        """
        places: list[RawPlace] = []
        seen_ids: set[str] = set()

        page_size = self._allowed_page_size(TWOGIS_PAGE_SIZE)
        for page in range(1, self._max_pages + 1):
            try:
                result = await self._fetch_page(query, area, page, page_size, max_attempts=1)
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
                self._max_pages, len(places), area.district, query.category,
            )

        return places


def parse_catalog_items(
    data: dict[str, Any],
    district_name: str | None,
    category_key: str,
    high_margin: bool,
    city_slug: str = DEFAULT_CITY_SLUG,
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

        phones, websites, socials_list, emails = extract_contacts(item)
        phone_str = ", ".join(phones) if phones else None
        raw_website = websites[0] if websites else None
        socials_str = ", ".join(socials_list) if socials_list else None

        if not socials_str and raw_website and is_social_domain(url_host(raw_website)):
            socials_str = raw_website

        # Places API has no field with this link, it is built from the id.
        source_url = build_card_url(city_slug, raw_id)
        if source_url is None:
            logger.warning("2GIS: cannot build a card link for id %r, the lead is saved without it", raw_id)

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
                source_url=source_url,
                rubric=_primary_rubric(item),
                contacts_known=bool(phones or websites or socials_list or emails),
            )
        )

    return places


def _primary_rubric(item: dict[str, Any]) -> str | None:
    """The name of the main rubric of an organization; if none is marked as main, the first one."""
    rubrics = item.get("rubrics")
    named = [
        rubric
        for rubric in (rubrics if isinstance(rubrics, list) else [])
        if isinstance(rubric, dict) and isinstance(rubric.get("name"), str) and rubric["name"].strip()
    ]
    if not named:
        return None
    primary = next((rubric for rubric in named if rubric.get("kind") == "primary"), named[0])
    return str(primary["name"]).strip()[:255]


def extract_contacts(
    item: dict[str, Any]
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
