"""Chrome CDP browser layer and 2GIS parsers ported from `parser-2gis-new`."""

from .browser import ChromeBrowser
from .dom import DOMNode
from .exceptions import (
    ChromeException,
    ChromePathNotFound,
    ChromeRuntimeException,
    ChromeUserAbortException,
    ProxyError,
)
from .filters import (
    FIRM_URL_PATTERN,
    IN_BUILDING_URL_PATTERN,
    ITEM_RESPONSE_PATTERN,
    SEARCH_URL_PATTERN,
)
from .options import ChromeOptions, ChromeParserOptions, default_memory_limit
from .parsers import (
    ChromeScraperSession,
    Gate,
    GateAction,
    GateDecision,
    InBuildingPageReader,
    ScrapedSearchResult,
    SearchPageReader,
    classify_2gis_url,
    firm_id_from_href,
    is_in_building_firm_link,
    is_search_result_link,
    strip_page_number,
)
from .proxy import ProxySettings, parse_proxy
from .remote import ChromeRemote, patch_pychrome
from .utils import ensure_local_cdp_url, floor_to_hundreds, free_port, locate_chrome_path, wait_until_finished

__all__ = [
    "FIRM_URL_PATTERN",
    "IN_BUILDING_URL_PATTERN",
    "ITEM_RESPONSE_PATTERN",
    "SEARCH_URL_PATTERN",
    "ChromeBrowser",
    "ChromeException",
    "ChromeOptions",
    "ChromeParserOptions",
    "ChromePathNotFound",
    "ChromeRemote",
    "ChromeRuntimeException",
    "ChromeScraperSession",
    "ChromeUserAbortException",
    "DOMNode",
    "Gate",
    "GateAction",
    "GateDecision",
    "InBuildingPageReader",
    "ProxyError",
    "ProxySettings",
    "ScrapedSearchResult",
    "SearchPageReader",
    "classify_2gis_url",
    "default_memory_limit",
    "ensure_local_cdp_url",
    "firm_id_from_href",
    "floor_to_hundreds",
    "free_port",
    "is_in_building_firm_link",
    "is_search_result_link",
    "locate_chrome_path",
    "parse_proxy",
    "patch_pychrome",
    "strip_page_number",
    "wait_until_finished",
]
