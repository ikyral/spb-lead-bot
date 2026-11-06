# Technical Specification: SPb Lead Finder Bot (spb-lead-bot)

## 1. Purpose
Find local businesses in Saint Petersburg (SPb) that need website development services:
- `no_site`: No website specified in map card.
- `social_only`: Only VK, Telegram, Taplink, YClients, or Dikidi link instead of a standalone website.
- `dead_site`: Website URL returns HTTP 4xx/5xx (except 401/403/429), SSL error, DNS failure, or timeout.

Site statuses (`leads.site_status`, src/services/site_status.py), in the order of handing out:
1. `no_site`, `dead_site`, `social_only`: a source, a check of the listed address, or a person's verdict.
2. `site_not_found`: an automatic search found nothing. Not proof of there being no site.
3. `site_uncertain`: there is a candidate in `site_url` that may belong to someone else.
4. `unknown`: nobody has looked, e.g. the source gives no contacts.

`site_found` (a working site is known) is never handed out. `has_site` is only the answer of the checker:
such a place is not saved. No automatic search exists yet, so nothing sets `site_not_found` or `site_uncertain`.

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
- `site_status`: String(32), Not Null (see section 1)
- `score`: Integer, Not Null, Default 0 (Range: 0..100)
- `crm_status`: String(32), Not Null, Default `new` (`new`, `in_work`, `rejected`, `issued` for handed-out leads)
- `map_url`: String(512), Nullable
- `created_at`: DateTime, Default UTC Now
- `email`: String(255), Nullable (only an address published in the public card)
- `source_url`: String(512), Nullable (the organization's card in the source)
- `issued_at`: DateTime, Nullable (when the lead was handed out to an admin)
- `contact_status`: String(32), Nullable (`contacted`, `replied`, `rejected`, `no_answer`)
- `contact_status_by_id`: BigInteger, Nullable (Telegram ID of the admin who set the status)
- `contact_status_by`: String(128), Nullable (their @username or name at that moment)
- `contact_status_at`: DateTime, Nullable (when the status was set)
- `exported_at`: DateTime, Nullable (on a collector's computer: when the lead went into an export file)
- `imported_at`: DateTime, Nullable (on the server: when the lead arrived from an export file)
- `site_url`: String(512), Nullable (the known site or the candidate)
- `site_confidence`: String(16), Nullable (`high`, `medium`, `low`)
- `site_source`: String(16), Nullable (`2gis`, `osm`, `search`, `manual`, `2gis_card`)
- `site_checked_at`: DateTime, Nullable (when the status was determined)
- `is_chain`: Boolean, Nullable (part of a chain: kept for counting branches, not handed out)

The `collector_state` table has a single row with the automatic collection switch, the result of the last run, the position in the search targets and the daily request count. The `import_log` table records each imported file with counts only, never the content of leads. The `provider_state` table holds blocks of each source: how many in a row, the pause after them, the automatic switch-off and the last run.

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
            "high_margin": False
        },
        # And small businesses, with OSM tags and queries in src/services/providers/base.py:
        # barbershop, nails, massage, tire_service, detailing, cleaning, renovation,
        # appliance_repair, workshop, tutors, photographers, florists.
    }

`hotels` and `horeca` are collected but ranked lower: see the scoring formula.

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
All weights are constants at the top of the module.
- If a working site is known (`has_site`, `site_found`) -> return `0`.
- Status base points: `dead_site` `+60`, `social_only` `+55`, `no_site` `+50`, `site_not_found` `+40`,
  `site_uncertain` `+35`, `unknown` `+30`.
- Contact points:
  - Russian mobile `phone`: `+25`; another `phone`: `+15`
  - Has `socials`: `+10`
  - Neither `phone` nor `socials`: `-35`, except unverified statuses (`unknown`, `site_uncertain`, `site_not_found`)
- Category:
  - High-margin category: `+10` (category key in `HIGH_MARGIN_CATEGORY_KEYS`, `high_margin: True`, or a free-text
    category such as a CSV `Рубрика` containing a keyword from `HIGH_MARGIN_KEYWORDS`).
  - Small repair shops (`ремонт обуви / одежды / телефонов / часов / ноутбуков / бытовой техники`, incl.
    `ремонт и пошив одежды`) get no bonus: these phrases are removed before keyword matching, so
    `Ремонт под ключ`, `Кузовной ремонт` and `Автосервис, ремонт телефонов` keep it.
  - `hotels`, `horeca`: `-10`.
- Two branches under one name: `-10`. Three or more, or a listed brand: a chain, not handed out at all.
- Clamp final score between `0` and `100`. Save leads with a verdict from `score >= 30`, unverified ones at any score.
- Existing leads keep their score until an admin runs `/rescore`.
