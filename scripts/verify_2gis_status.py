#!/usr/bin/env python3
"""
Dedicated verification script for 2GIS parser and scraper components.
Checks:
  1. Offline parser mechanics (DOM node filtering, stat base64 decode, URL classification).
  2. Synthetic mock Chrome session execution (click -> byid interception -> contact extraction).
  3. Optional live search check against 2GIS web (when --live flag is passed).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.api_client import ProviderNoConnectionError
from src.services.providers.twogis import parse_catalog_items
from src.services.providers.twogis_scraper import TwoGisScraperProvider
from src.services.scraper.chrome.dom import DOMNode
from src.services.scraper.chrome.parsers import (
    classify_2gis_url,
    is_in_building_firm_link,
    is_search_result_link,
    strip_page_number,
)
from src.services.scraper.chrome.utils import locate_chrome_path


def run_offline_checks() -> bool:
    print("=== [1/2] Running Offline Parser Component Checks ===")
    all_ok = True

    # 1. URL classification
    assert classify_2gis_url("https://2gis.ru/spb/inside/700300761") == "in_building"
    assert classify_2gis_url("https://2gis.ru/spb/firm/700000010") == "firm"
    assert classify_2gis_url("https://2gis.ru/spb/search/кафе") == "search"
    print("  [OK] URL classifier correctly identifies search, firm, and in_building URLs")

    # 2. Pagination URL cleaning
    assert strip_page_number("https://2gis.ru/spb/search/cafe/page/6?m=1") == "https://2gis.ru/spb/search/cafe?m=1"
    print("  [OK] Page suffix stripper normalizes pagination URLs for anti-bot bypass")

    # 3. DOM node link validator with stat= base64
    valid_stat = base64.b64encode(b'{"test": 1}').decode("ascii")
    url_encoded_stat = valid_stat.replace("=", "%3D")
    valid_node = DOMNode.model_validate({
        "nodeId": 1,
        "backendNodeId": 10,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", f"/spb/firm/123456?stat={valid_stat}"],
        "children": [],
    })
    valid_encoded_node = DOMNode.model_validate({
        "nodeId": 3,
        "backendNodeId": 30,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", f"/spb/firm/123456?stat={url_encoded_stat}"],
        "children": [],
    })
    urlsafe_node = DOMNode.model_validate({
        "nodeId": 4,
        "backendNodeId": 40,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456?stat=-__-"],
        "children": [],
    })
    invalid_node = DOMNode.model_validate({
        "nodeId": 2,
        "backendNodeId": 20,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456?stat=not_valid_base64!!!"],
        "children": [],
    })
    whitespace_node = DOMNode.model_validate({
        "nodeId": 5,
        "backendNodeId": 50,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456?stat=%20"],
        "children": [],
    })
    empty_node = DOMNode.model_validate({
        "nodeId": 6,
        "backendNodeId": 60,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456?stat="],
        "children": [],
    })
    assert is_search_result_link(valid_node) is True
    assert is_search_result_link(valid_encoded_node) is True
    assert is_search_result_link(urlsafe_node) is True
    assert is_search_result_link(invalid_node) is False
    assert is_search_result_link(whitespace_node) is False
    assert is_search_result_link(empty_node) is False

    none_attr_node = DOMNode.model_validate({
        "nodeId": 7,
        "backendNodeId": 70,
        "nodeType": 1,
        "nodeName": "DIV",
        "localName": "div",
        "nodeValue": "",
        "attributes": None,
        "children": [],
    })
    assert none_attr_node.attributes == {}

    in_bldg_valid = DOMNode.model_validate({
        "nodeId": 10,
        "backendNodeId": 100,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456"],
        "children": [],
    })
    in_bldg_invalid = DOMNode.model_validate({
        "nodeId": 11,
        "backendNodeId": 110,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456/reviews"],
        "children": [],
    })
    in_bldg_utm = DOMNode.model_validate({
        "nodeId": 12,
        "backendNodeId": 120,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456?utm_source=2gis/web"],
        "children": [],
    })
    in_bldg_slash = DOMNode.model_validate({
        "nodeId": 13,
        "backendNodeId": 130,
        "nodeType": 1,
        "nodeName": "A",
        "localName": "a",
        "nodeValue": "",
        "attributes": ["href", "/spb/firm/123456/"],
        "children": [],
    })
    assert is_in_building_firm_link(in_bldg_valid) is True
    assert is_in_building_firm_link(in_bldg_utm) is True
    assert is_in_building_firm_link(in_bldg_slash) is True
    assert is_in_building_firm_link(in_bldg_invalid) is False
    print("  [OK] DOM selector correctly filters links by base64 stat parameter and in-building firm paths")

    # 4. Chrome binary detection
    chrome_path = locate_chrome_path()
    if chrome_path:
        print(f"  [OK] Located Chrome executable: {chrome_path}")
    else:
        print("  [WARN] No Chrome executable found on standard paths or Playwright cache")
        all_ok = False

    # 5. Synthetic lead extraction from mock /items/byid payload
    sample_payload = {
        "meta": {"code": 200},
        "result": {
            "items": [
                {
                    "id": "70000001019608005",
                    "name": "Горздрав, сеть аптек",
                    "type": "branch",
                    "point": {"lat": 59.93, "lon": 30.31},
                    "address_name": "Невский проспект, 100",
                    "contact_groups": [
                        {
                            "contacts": [
                                {"type": "phone", "value": "+7 (812) 555-01-23"},
                                {"type": "website", "value": "https://gorzdrav.org"},
                            ]
                        }
                    ],
                }
            ]
        },
    }
    extracted_items = TwoGisScraperProvider._extract_byid_items([sample_payload])
    assert len(extracted_items) == 1
    assert extracted_items[0]["name"] == "Горздрав, сеть аптек"

    parsed_places = parse_catalog_items(
        sample_payload,
        "Центральный",
        "pharmacy",
        True,
        city_slug="spb",
    )
    from src.services.normalize import normalize_phone

    assert len(parsed_places) == 1
    place = parsed_places[0]
    assert place.name == "Горздрав, сеть аптек"
    assert place.phone == "+7 (812) 555-01-23"
    assert normalize_phone(place.phone) == "+78125550123"
    assert place.raw_website == "https://gorzdrav.org"
    assert place.district == "Центральный"
    print("  [OK] Synthetic /items/byid interception and lead normalization verified")

    return all_ok


async def run_live_check(query_url: str = "https://2gis.ru/moscow/search/Аптеки") -> bool:
    print(f"\n=== [2/2] Running Live 2GIS Chrome CDP Scraper Verification ({query_url}) ===")
    from src.services.scraper.chrome.parsers import ChromeScraperSession

    session = ChromeScraperSession(headless=True)
    try:
        await session.start()
        print("  Chrome CDP session started successfully.")
        res = await session.read_search(query_url, page=1, max_items=2)
        print(f"  Scrape completed: {len(res.items)} items extracted.")
        if res.blocked_reason:
            print(f"  [WARN] Block detected: {res.blocked_reason}")
            return False
        if res.items:
            for idx, item in enumerate(res.items, start=1):
                raw_item = item.get("result", {}).get("items", [{}])[0]
                name = raw_item.get("name")
                print(f"    Item {idx}: {name} (id={raw_item.get('id')})")
            print("  [OK] Live 2GIS Chrome CDP parser successfully intercepted and parsed /items/byid")
            return True
        else:
            print("  [INFO] 0 items parsed (possible captcha or empty results)")
            return False
    except ProviderNoConnectionError as exc:
        print(f"  [WARN] Live network connection unavailable: {exc}")
        print("  (Offline environment detected: live 2GIS network query skipped cleanly)")
        return True
    except Exception as exc:
        print(f"  [FAIL] Scraper run crashed: {exc}")
        return False
    finally:
        await session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify 2GIS parser and scraper operational status.")
    parser.add_argument("--live", action="store_true", help="Perform live query test against 2gis.ru")
    args = parser.parse_args()

    offline_ok = run_offline_checks()
    if not offline_ok:
        sys.exit(1)

    if args.live:
        live_ok = asyncio.run(run_live_check())
        if not live_ok:
            sys.exit(1)

    print("\nVerification successfully completed.")


if __name__ == "__main__":
    main()
