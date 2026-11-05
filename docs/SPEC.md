# Technical Specification: SPb Lead Finder Bot (spb-lead-bot)

## 1. Purpose
Find local businesses in Saint Petersburg (SPb) that need website development services:
- `no_site`: No website specified in map card.
- `social_only`: Only VK, Telegram, Taplink, YClients, or Dikidi link instead of a standalone website.
- `dead_site`: Website URL returns HTTP 4xx/5xx (except 401/403/429), SSL error, DNS failure, or timeout.

## 2. Database Schema (leads table in SQLite)
- `id`: Integer, Primary Key, Autoincrement
- `external_id`: String(128), Unique, Indexed (Format: `osm_{id}`, `2gis_{id}`, `csv_{hash}`)
- `source`: String(32) (`osm`, `2gis`, `csv`)
- `name`: String(255), Not Null
- `category`: String(128), Not Null
- `district`: String(128), Nullable
- `address`: String(512), Nullable
- `phone`: String(128), Nullable
- `socials`: String(512), Nullable
- `raw_website`: String(512), Nullable
- `site_status`: String(32), Not Null (`no_site`, `social_only`, `dead_site`, `has_site`)
- `score`: Integer, Not Null, Default 0 (Range: 0..100)
- `crm_status`: String(32), Not Null, Default `new` (`new`, `in_work`, `rejected`, and for handed-out leads `issued`, `contacted`, `replied`, `no_answer`)
- `map_url`: String(512), Nullable
- `created_at`: DateTime, Default UTC Now
- `email`: String(255), Nullable (only an address published in the public card)
- `source_url`: String(512), Nullable (the organization's card in the source)
- `issued_at`: DateTime, Nullable (when the lead was handed out to an admin)

The `collector_state` table has a single row with the automatic collection switch, the result of the last run, the position in the search targets and the daily request count.

## 3. Saint Petersburg Districts Bounding Boxes (SPB_DISTRICTS)
Format: `(south_lat, west_lon, north_lat, east_lon)`

    SPB_DISTRICTS = {
        "Центральный": (59.915, 30.305, 59.955, 30.395),
        "Петроградский": (59.945, 30.250, 59.985, 30.345),
        "Василеостровский": (59.920, 30.200, 59.960, 30.305),
        "Адмиралтейский": (59.895, 30.255, 59.935, 30.340),
        "Московский": (59.810, 30.280, 59.905, 30.365),
        "Приморский": (59.975, 30.140, 60.060, 30.320),
        "Выборгский": (59.970, 30.290, 60.090, 30.400),
        "Калининский": (59.955, 30.350, 60.045, 30.460),
        "Фрунзенский": (59.820, 30.340, 59.915, 30.440),
        "Невский": (59.830, 30.410, 59.935, 30.530),
    }

## 4. Target Categories & OSM / 2GIS Mapping (TARGET_CATEGORIES)

    TARGET_CATEGORIES = {
        "dentist_clinic": {
            "title": "🦷 Стоматологии и клиники",
            "osm_tags": [("amenity", "dentist"), ("amenity", "clinic"), ("amenity", "doctors")],
            "query_2gis": "стоматология клиника",
            "high_margin": True
        },
        "auto_repair": {
            "title": "🚗 Автосервисы и детейлинг",
            "osm_tags": [("shop", "car_repair"), ("craft", "car_painter")],
            "query_2gis": "автосервис кузовной ремонт детейлинг",
            "high_margin": True
        },
        "beauty": {
            "title": "💇‍♀️ Салоны красоты и косметология",
            "osm_tags": [("shop", "beauty"), ("shop", "hairdresser")],
            "query_2gis": "салон красоты косметология",
            "high_margin": True
        },
        "legal_b2b": {
            "title": "⚖️ Юристы, бухучет и B2B",
            "osm_tags": [("office", "lawyer"), ("office", "accountant"), ("office", "company")],
            "query_2gis": "юридические услуги бухгалтерские услуги",
            "high_margin": True
        },
        "horeca": {
            "title": "☕ Кафе, рестораны и пекарни",
            "osm_tags": [("amenity", "restaurant"), ("amenity", "cafe")],
            "query_2gis": "ресторан кафе",
            "high_margin": False
        },
        "hotels": {
            "title": "🏨 Отели и базы отдыха",
            "osm_tags": [("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "chalet")],
            "query_2gis": "отель гостиница база отдыха",
            "high_margin": True
        }
    }

## 5. Website Classification Rules (src/services/checker.py)
- Social/Aggregator domains (`social_only`): `vk.com`, `vk.ru`, `t.me`, `taplink.cc`, `wa.me`, `instagram.com`, `yclients.com`, `dikidi.net`, `linktr.ee`.
- If URL is empty/None -> `no_site`.
- Perform `GET` request with `httpx.AsyncClient(timeout=7.0, follow_redirects=True)` and standard browser `User-Agent`; the whole check is capped at 20s. The response body is not downloaded.
- URL without scheme is tried as `https://` first; if that fails with a connection/SSL error or a dead status, it is retried as `http://` (no retry after a timeout).
- If status code >= 400 (except `401`, `403`, `429`: the server is alive but blocks bots / requires auth / rate-limits) or raises `httpx.HTTPError` -> `dead_site`.
- Connections to non-public addresses (loopback, private, link-local incl. `169.254.169.254`, CGNAT, multicast, reserved; IPv4 and IPv6) are refused for every hop including redirects, after DNS resolution -> treated as a connection error (`dead_site`). Env proxies are ignored. See `src/services/safe_http.py`.
- If final redirected URL host matches social/aggregator domains -> `social_only`.
- Otherwise -> `has_site`.

## 6. Lead Scoring Formula (src/services/scorer.py)
- If `site_status == "has_site"` -> return `0`.
- Status base points:
  - `dead_site`: `+45`
  - `social_only`: `+35`
  - `no_site`: `+25`
- Contact points:
  - Has `phone`: `+25`
  - Has `socials`: `+15`
  - Neither `phone` nor `socials`: `-30`
- Category bonus:
  - High-margin category: `+15` (category key with `high_margin: True`, or a free-text category such as a CSV
    `Рубрика` containing a keyword from `HIGH_MARGIN_KEYWORDS`).
  - Small repair shops (`ремонт обуви / одежды / телефонов / часов / ноутбуков / бытовой техники`, incl.
    `ремонт и пошив одежды`) get no bonus: these phrases are removed before keyword matching, so
    `Ремонт под ключ`, `Кузовной ремонт` and `Автосервис, ремонт телефонов` keep it.
- Clamp final score between `0` and `100`. Only save leads with `score >= 30`.
