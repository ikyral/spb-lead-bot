"""CSV lead parser supporting flexible encodings, delimiters, and column names."""

import csv
import hashlib
import io
import httpx

from src.services.checker import is_social_domain
from src.services.providers.base import RawPlace
from src.services.scorer import HIGH_MARGIN_KEYWORDS

COLUMN_MAPPING: dict[str, str] = {
    "name": "name",
    "название": "name",
    "имя": "name",
    "category": "category",
    "рубрика": "category",
    "категория": "category",
    "district": "district",
    "район": "district",
    "address": "address",
    "адрес": "address",
    "phone": "phone",
    "телефон": "phone",
    "телефоны": "phone",
    "website": "website",
    "сайт": "website",
    "web": "website",
    "socials": "socials",
    "вк": "socials",
    "vk": "socials",
    "telegram": "socials",
}


def _detect_delimiter(first_line: str) -> str:
    """Detect CSV delimiter between ';' and ',' based on frequency."""
    semicolons = first_line.count(";")
    commas = first_line.count(",")
    return ";" if semicolons >= commas and semicolons > 0 else ","


def parse_csv_leads(file_bytes: bytes) -> list[RawPlace]:
    """
    Parse leads from raw CSV bytes with auto-encoding and auto-delimiter detection.

    Returns a list of RawPlace objects with stable MD5 external_ids.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("cp1251", errors="replace")

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    delimiter = _detect_delimiter(lines[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    places: list[RawPlace] = []
    seen_ids: set[str] = set()

    for row in reader:
        collected: dict[str, list[str]] = {
            "name": [],
            "category": [],
            "district": [],
            "address": [],
            "phone": [],
            "website": [],
            "socials": [],
        }

        for col_name, raw_val in row.items():
            if not col_name or not raw_val:
                continue
            cleaned_col = str(col_name).strip().lower()
            field_name = COLUMN_MAPPING.get(cleaned_col)
            if field_name:
                val_str = str(raw_val).strip()
                if val_str:
                    collected[field_name].append(val_str)

        name = collected["name"][0] if collected["name"] else ""
        if not name:
            continue

        category = collected["category"][0] if collected["category"] else "other"
        district = collected["district"][0] if collected["district"] else None
        address = collected["address"][0] if collected["address"] else None
        phone = ", ".join(collected["phone"]) if collected["phone"] else None
        raw_website = collected["website"][0] if collected["website"] else None
        socials = ", ".join(collected["socials"]) if collected["socials"] else None

        if not socials and raw_website:
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
                socials = raw_website

        norm_cat = category.lower()
        is_high = any(keyword in norm_cat for keyword in HIGH_MARGIN_KEYWORDS)

        key_str = f"{name}_{phone}_{address}"
        external_id = f"csv_{hashlib.md5(key_str.encode('utf-8')).hexdigest()[:12]}"
        if external_id in seen_ids:
            continue
        seen_ids.add(external_id)

        places.append(
            RawPlace(
                external_id=external_id,
                source="csv",
                name=name,
                category=category,
                district=district,
                address=address,
                phone=phone,
                socials=socials,
                raw_website=raw_website,
                map_url=None,
                high_margin=is_high,
            )
        )

    return places
