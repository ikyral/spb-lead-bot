"""
What the code knows about the pages of Google search and about sites of organizations. No network here.

Reading a results page is a pure function with several ways to find the results, from the most
specific to the most general. If none of them understands the page, it says so: a page that was
not understood must never pass for a search that found nothing.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlsplit

from src.services.checker import is_catalog_domain, is_social_domain, url_host
from src.services.normalize import normalize_email, normalize_phone

SEARCH_URL: str = "https://www.google.com/search"
# Only the top of the results: further down a page is less and less likely to be about this organization.
MAX_RESULTS_LOOKED_AT: int = 5
CITY: str = "Санкт-Петербург"

PAGE_RESULTS = "results"
PAGE_NOTHING_FOUND = "nothing_found"
PAGE_CAPTCHA = "captcha"
PAGE_CONSENT = "consent"
PAGE_REFUSED = "refused"
PAGE_SERVER_ERROR = "server_error"
PAGE_UNRECOGNIZED = "unrecognized"

_REFUSAL_STATUSES: frozenset[int] = frozenset({403, 429})
_SERVER_ERROR: int = 500
# Signs of the page Google shows instead of results when it suspects a robot.
_CAPTCHA_MARKERS: tuple[str, ...] = (
    'id="captcha-form"',
    "g-recaptcha",
    "unusual traffic from your computer network",
    "our systems have detected unusual traffic",
    "необычный трафик",
    "подозрительный трафик",
)
_CONSENT_HOST: str = "consent.google.com"
_NOTHING_FOUND_MARKERS: tuple[str, ...] = (
    "ничего не найдено",
    "did not match any documents",
    "no results found for",
)
# Where the results are: the ids of Google's blocks, from the narrowest one.
_RESULT_CONTAINERS: tuple[str, ...] = ("rso", "search")
_SEARCH_TITLE_MARKERS: tuple[str, ...] = ("google", "поиск")

_WORD_RE = re.compile(r"[0-9a-zа-я]+")
_LEGAL_FORMS: frozenset[str] = frozenset({"ооо", "ип", "ао", "зао", "оао", "пао", "ано", "нко", "llc", "ltd", "inc"})
# Words of a niche, not of a name: 'Стоматология Улыбка' is about 'улыбка'.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "стоматология",
        "стоматологическая",
        "стоматологический",
        "стоматологии",
        "клиника",
        "клиники",
        "медицинский",
        "медицинская",
        "центр",
        "салон",
        "красоты",
        "студия",
        "автосервис",
        "автотехцентр",
        "сервис",
        "шиномонтаж",
        "кафе",
        "ресторан",
        "бар",
        "кофейня",
        "пекарня",
        "гостиница",
        "отель",
        "хостел",
        "мини",
        "юридическая",
        "компания",
        "бюро",
        "агентство",
        "и",
        "в",
        "на",
        "спб",
        "санкт",
        "петербург",
    }
)
# A page about an organization rather than its own: catalogs that are not on the list still title themselves so.
_CATALOG_TITLE_WORDS: tuple[str, ...] = ("отзывы", "рейтинг", "на карте", "адрес и телефон", "телефон и адрес")
_TRANSLIT: dict[str, str] = {
    **dict(zip("абвгдеёзийклмнопрстуфхыэ", "abvgdeeziyklmnoprstufhye", strict=True)),
    "ж": "zh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ь": "",
    "ю": "yu",
    "я": "ya",
}
# Letters a word matches by: one typo or another way to write a letter in Latin is still the same word.
_WORD_MATCH_RATIO: float = 0.85
_MIN_WORD_LENGTH: int = 3

_PHONE_TEXT_RE = re.compile(r"(?:\+7|\b8)[\s\-‐–(]*\d{3}[\s\-‐–)]*\d{3}[\s\-‐–]*\d{2}[\s\-‐–]*\d{2}\b")
_EMAIL_TEXT_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_NOT_EMAIL_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
_NOT_EMAIL_DOMAINS: tuple[str, ...] = ("example.com", "example.ru", "sentry.io", "wixpress.com", "domain.ru")
_MAX_PHONES: int = 5
_MAX_SOCIALS: int = 5
# Hosts of the networks that count as contacts, and paths on them that are not a page of anyone.
_CONTACT_HOSTS: tuple[str, ...] = ("instagram.com", "t.me", "wa.me", "whatsapp.com", "vk.com", "vk.ru")
_NOT_A_PAGE_PATHS: tuple[str, ...] = ("share", "share.php", "widget", "js", "intent", "sharer", "send")


@dataclass(frozen=True)
class SearchResult:
    """One result on a page: where it leads and what it is titled."""

    url: str
    title: str


@dataclass
class ResultsPage:
    """What a page of Google turned out to be, and its results if it is a page of results."""

    kind: str
    results: list[SearchResult] = field(default_factory=list)
    # For the log: which way of reading found the results.
    layout: str | None = None


@dataclass(frozen=True)
class Choice:
    """What the top of the results says about an organization."""

    website: str | None
    socials: list[str]


@dataclass(frozen=True)
class SiteContacts:
    """What the page of a site lists."""

    phones: list[str]
    socials: list[str]
    email: str | None


# ---------- the search ----------


def search_text(name: str, address: str | None, district: str | None) -> str:
    """'<name> <address> Санкт-Петербург'. Without an address the district is better than nothing."""
    place = address or district or ""
    parts = [name, place] if place else [name]
    if CITY.lower() not in place.lower():
        parts.append(CITY)
    return " ".join(" ".join(parts).split())


def build_search_url(text: str) -> str:
    return f"{SEARCH_URL}?q={quote_plus(text)}&hl=ru"


def _is_google_host(host: str | None) -> bool:
    labels = (host or "").lower().rstrip(".").split(".")
    return "google" in labels[-3:-1] or (host or "").endswith(("googleusercontent.com", "gstatic.com"))


def read_results_page(status: int | None, final_url: str, title: str, html: str) -> ResultsPage:
    """
    Tell what Google showed. Refusals come first: a captcha may arrive with any status and any title.

    PAGE_UNRECOGNIZED means the code does not understand this page: it is not a refusal and not an empty search.
    """
    lowered = html.lower()
    parts = urlsplit(final_url)
    if parts.path.startswith("/sorry") or any(marker in lowered for marker in _CAPTCHA_MARKERS):
        return ResultsPage(PAGE_CAPTCHA)
    if parts.hostname == _CONSENT_HOST:
        return ResultsPage(PAGE_CONSENT)
    if status in _REFUSAL_STATUSES:
        return ResultsPage(PAGE_REFUSED)
    if status is not None and status >= _SERVER_ERROR:
        return ResultsPage(PAGE_SERVER_ERROR)

    found = _ResultsParser.read(html)
    for container in _RESULT_CONTAINERS:
        if container in found.containers:
            results = found.results.get(container, [])
            if results:
                return ResultsPage(PAGE_RESULTS, results, layout=f"#{container}")
            if any(marker in lowered for marker in _NOTHING_FOUND_MARKERS):
                return ResultsPage(PAGE_NOTHING_FOUND, layout=f"#{container}")
    # A banner about cookies over the results does not stop anything; a page with nothing but the question does.
    if f"//{_CONSENT_HOST}" in lowered:
        return ResultsPage(PAGE_CONSENT)
    # The last way: titled links anywhere, but only on a page that says it is a search of Google.
    anywhere = found.results.get("", [])
    if anywhere and any(marker in title.lower() for marker in _SEARCH_TITLE_MARKERS):
        return ResultsPage(PAGE_RESULTS, anywhere, layout="page")
    return ResultsPage(PAGE_UNRECOGNIZED)


def _result_url(href: str) -> str | None:
    """The address a result leads to: Google may wrap it into its own '/url?q=' link."""
    if href.startswith("/url?"):
        query = parse_qs(urlsplit(href).query)
        href = (query.get("q") or query.get("url") or [""])[0]
    if not href.startswith(("http://", "https://")):
        return None
    return None if _is_google_host(url_host(href)) else href


class _ResultsParser(HTMLParser):
    """
    Titled links: '<a href><h3>title</h3></a>' and the older '<h3><a href>title</a></h3>'.

    Each is remembered in every container around it, and in '' for the page as a whole.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.containers: set[str] = set()
        self.results: dict[str, list[SearchResult]] = {}
        # Open tags with the container they open, if any: the nesting tells when a container ends.
        self._stack: list[tuple[str, str | None]] = []
        self._link: str | None = None
        self._link_depth = 0
        self._heading_depth: int | None = None
        self._heading_link: str | None = None
        self._text: list[str] = []

    @classmethod
    def read(cls, html: str) -> "_ResultsParser":
        parser = cls()
        parser.feed(html)
        parser.close()
        return parser

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        container = values.get("id") if values.get("id") in _RESULT_CONTAINERS else None
        if container:
            self.containers.add(container)
        if tag in _VOID_TAGS:
            return
        self._stack.append((tag, container))
        if tag == "a":
            self._link = values.get("href")
            self._link_depth = len(self._stack)
        elif tag == "h3" and self._heading_depth is None:
            self._heading_depth = len(self._stack)
            self._heading_link = self._link
            self._text = []
        if tag == "a" and self._heading_depth is not None and self._heading_link is None:
            self._heading_link = self._link

    def handle_data(self, data: str) -> None:
        if self._heading_depth is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not any(open_tag == tag for open_tag, _ in self._stack):
            return
        while self._stack:
            depth = len(self._stack)
            open_tag, _ = self._stack[-1]
            if self._heading_depth == depth:
                self._finish_heading()
            if self._link_depth == depth:
                self._link, self._link_depth = None, 0
            self._stack.pop()
            if open_tag == tag:
                return

    def _finish_heading(self) -> None:
        title = " ".join("".join(self._text).split())
        url = _result_url(self._heading_link or "")
        self._heading_depth, self._heading_link = None, None
        if not title or url is None:
            return
        result = SearchResult(url=url, title=title)
        for key in ("", *(container for _, container in self._stack if container)):
            listed = self.results.setdefault(key, [])
            if result.url not in (known.url for known in listed):
                listed.append(result)


_VOID_TAGS: frozenset[str] = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


# ---------- is a page about this organization ----------


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return [word for word in _WORD_RE.findall(folded) if word not in _LEGAL_FORMS]


def _latin(word: str) -> str:
    return "".join(_TRANSLIT.get(char, char) for char in word)


def name_words(name: str, niche_title: str | None = None) -> list[str]:
    """The words that tell this organization from others of its niche. All of them if the name is only generic."""
    generic = _GENERIC_WORDS | set(_words(niche_title or ""))
    words = [word for word in _words(name) if len(word) >= 2]
    distinctive = [word for word in words if word not in generic]
    return distinctive or words


def _word_in(word: str, candidates: list[str]) -> bool:
    return any(
        candidate == word or SequenceMatcher(None, word, candidate).ratio() >= _WORD_MATCH_RATIO
        for candidate in candidates
    )


def _word_in_label(word: str, label: str) -> bool:
    """Is the word written in a domain or a nickname: 'улыбка' in 'ulybka-dent', with some spelling freedom."""
    latin = _latin(word)
    if len(latin) < _MIN_WORD_LENGTH:
        return False
    if latin in label:
        return True
    size = len(latin)
    return any(
        SequenceMatcher(None, latin, label[start : start + size]).ratio() >= _WORD_MATCH_RATIO
        for start in range(max(1, len(label) - size + 1))
    )


def similarity(words: list[str], title: str, label: str) -> float:
    """
    The share of the name's words found in the title, or in the label (a domain or a nickname), whichever is more.

    0 without words: nothing to compare means nothing is confirmed.
    """
    if not words:
        return 0.0
    title_words = [*_words(title), *(_latin(word) for word in _words(title))]
    in_title = sum(1 for word in words if _word_in(word, title_words) or _word_in(_latin(word), title_words))
    clean_label = re.sub(r"[^0-9a-z]", "", label.lower())
    in_label = sum(1 for word in words if _word_in_label(word, clean_label))
    if any(marker in title.lower() for marker in _CATALOG_TITLE_WORDS):
        # Such a title names the organization on someone else's page: only the address can say whose it is.
        return in_label / len(words)
    return max(in_title, in_label) / len(words)


def _domain_label(host: str) -> str:
    """The part of a host that is a name: 'ulybka-dent' of 'www.ulybka-dent.spb.ru'."""
    labels = host.lower().removeprefix("www.").split(".")
    return ".".join(labels[:-1]) if len(labels) > 1 else labels[0]


def _social_label(url: str) -> str:
    """The nickname in a link to a network: 'ulybka_spb' of 'https://instagram.com/ulybka_spb/'."""
    path = [part for part in urlsplit(url).path.split("/") if part]
    return path[0] if path else ""


def site_of(url: str) -> str:
    """The main page of a site: a result often leads to an inner page."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def choose(results: list[SearchResult], name: str, threshold: float, niche_title: str | None = None) -> Choice:
    """
    The site of the organization and its pages on networks among the top results.

    A site is the first result similar enough by name that is neither a network nor a catalog.
    """
    words = name_words(name, niche_title)
    website: str | None = None
    socials: list[str] = []
    for result in results[:MAX_RESULTS_LOOKED_AT]:
        host = url_host(result.url)
        if host is None or is_catalog_domain(host) or _is_google_host(host):
            continue
        if is_social_domain(host):
            social = social_link(result.url)
            if social and similarity(words, result.title, _social_label(result.url)) >= threshold:
                socials.append(social)
            continue
        if website is None and similarity(words, result.title, _domain_label(host)) >= threshold:
            website = site_of(result.url)
    return Choice(website=website, socials=list(dict.fromkeys(socials))[:_MAX_SOCIALS])


# ---------- contacts on a site ----------


def _without_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _is_contact_page(url: str) -> bool:
    """A page of someone on a network or a booking service, not a button to share or the network itself."""
    path = [part.lower() for part in urlsplit(url).path.split("/") if part]
    return bool(path) and path[0] not in _NOT_A_PAGE_PATHS


def social_link(url: str) -> str | None:
    """A page on a network or a booking service in one form, whether it came from the results or from a site."""
    if not _is_contact_page(url):
        return None
    return _contact_link(url) or _without_query(url).rstrip("/")


def _contact_link(href: str) -> str | None:
    """A link to a network that is a way to reach the organization, in a form without a ready text."""
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    known_host = any(host == known or host.endswith(f".{known}") for known in _CONTACT_HOSTS)
    if parts.scheme not in ("http", "https") or not known_host:
        return None
    if host.endswith("whatsapp.com"):
        # 'api.whatsapp.com/send?phone=7...' carries the number in its query: keep it as a short link.
        phone = (parse_qs(parts.query).get("phone") or [""])[0]
        digits = re.sub(r"\D", "", phone)
        return f"https://wa.me/{digits}" if digits else None
    if not _is_contact_page(href):
        return None
    return f"https://{host}{parts.path}".rstrip("/")


class _LinksAndText(HTMLParser):
    """Links and the visible text of a page; scripts and styles are not text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._hidden += 1
        href = dict(attrs).get("href")
        if tag == "a" and href:
            self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.text.append(data)


def _email(value: str) -> str | None:
    email = normalize_email(value.split("?", 1)[0])
    if email is None or email.endswith(_NOT_EMAIL_SUFFIXES) or email.split("@", 1)[1] in _NOT_EMAIL_DOMAINS:
        return None
    return email


def extract_site_contacts(html: str) -> SiteContacts:
    """Phones, an email and pages on networks a site lists: from its links first, then from its text."""
    page = _LinksAndText()
    page.feed(html)
    page.close()
    text = " ".join(page.text)

    phones: list[str] = []
    emails: list[str] = []
    socials: list[str] = []
    for href in page.links:
        lowered = href.strip().lower()
        if lowered.startswith("tel:"):
            phones.append(normalize_phone(href.strip()[4:]) or "")
        elif lowered.startswith("mailto:"):
            emails.append(_email(href.strip()[7:]) or "")
        else:
            socials.append(_contact_link(href) or "")
    phones += [normalize_phone(match) or "" for match in _PHONE_TEXT_RE.findall(text)]
    emails += [_email(match) or "" for match in _EMAIL_TEXT_RE.findall(text)]

    def kept(values: list[str], limit: int) -> list[str]:
        return [value for value in dict.fromkeys(values) if value][:limit]

    found_emails = kept(emails, 1)
    return SiteContacts(
        phones=kept(phones, _MAX_PHONES),
        socials=kept(socials, _MAX_SOCIALS),
        email=found_emails[0] if found_emails else None,
    )
