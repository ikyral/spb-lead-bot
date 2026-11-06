"""
Yandex "Search by organizations" API: intentionally a stub.

The API terms do not fit how this bot works (https://yandex.ru/dev/tariffs/doc/ru/geosearch/terms/):
responses may be cached for at most 30 days and must not be stored otherwise (2.1, 2.2),
data may be shown only on top of a Yandex map (1.1). The bot stores leads indefinitely and
sends them to Telegram, so a working provider would break these terms.

To add it after getting written permission from Yandex (maps-api@support.yandex.ru): request
https://search-maps.yandex.ru/v1/ through ApiClient (type=biz, text, bbox, results, skip,
apikey), convert features to RawPlace with source="yandex", and return them from search().
Nothing else has to change: the collector works with any BaseMapProvider.
"""

from src.services.api_client import ProviderResponseError
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery

YANDEX_DISABLED_REASON: str = (
    "условия API Яндекса запрещают хранить результаты дольше 30 дней и показывать их вне Яндекс Карт"
)


class YandexProvider(BaseMapProvider):
    """Keeps the provider interface ready; never contacts Yandex."""

    name = "yandex"

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50) -> SearchPage:
        raise ProviderResponseError(f"Яндекс: провайдер не реализован ({YANDEX_DISABLED_REASON})")
