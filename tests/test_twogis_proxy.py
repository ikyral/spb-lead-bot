"""The proxy of the browser of 2GIS: parsing, masking, settings, the exit IP check and the stop of the run."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import collector_cli
from src.bot.notifier import AdminNotifier
from src.config import ProxySettingsError, Settings
from src.database.repo import get_provider_states
from src.services import pipeline
from src.services.api_client import ProviderProxyError
from src.services.collector import load_collector_config
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.registry import ProviderSet
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider
from src.services.scheduler import TRIGGER_MANUAL, CollectorRunner, RunReport
from src.services.scraper import robots
from src.services.scraper.chrome import ChromeBrowser, ChromeOptions, ChromeRemote, ChromeScraperSession, ProxyError
from src.services.scraper.chrome.proxy import extract_ip_info, normalize_country, parse_proxy
from src.services.scraper.chrome.remote import _PROXY_AUTH_MAX_ATTEMPTS

PASSWORD = "p@ss:w/rd#1"  # noqa: S105  # a made-up test value
ENCODED = "p%40ss%3Aw%2Frd%231"
PROXY_URL = f"http://user:{ENCODED}@proxy.example:3128"
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")


# ---------- parsing and masking ----------


@pytest.mark.parametrize(
    ("text", "scheme", "host", "port"),
    [
        ("proxy.example:3128", "http", "proxy.example", 3128),
        ("http://proxy.example:3128", "http", "proxy.example", 3128),
        ("HTTPS://proxy.example:443", "https", "proxy.example", 443),
        ("socks5://10.0.0.1:1080", "socks5", "10.0.0.1", 1080),
        ("socks5h://10.0.0.1:1080", "socks5", "10.0.0.1", 1080),
        ("  http://proxy.example:3128  ", "http", "proxy.example", 3128),
    ],
)
def test_proxy_forms_without_a_login(text: str, scheme: str, host: str, port: int) -> None:
    proxy = parse_proxy(text)
    assert (proxy.scheme, proxy.host, proxy.port) == (scheme, host, port)
    assert not proxy.has_auth
    assert proxy.server == f"{scheme}://{host}:{port}"


def test_special_characters_of_the_password_in_percent_form() -> None:
    proxy = parse_proxy(PROXY_URL)
    assert (proxy.username, proxy.password) == ("user", PASSWORD)
    assert proxy.has_auth
    assert proxy.server == "http://proxy.example:3128"  # no credentials: Chrome ignores them in this flag


def test_a_plain_at_sign_in_the_password_is_the_last_one_that_counts() -> None:
    proxy = parse_proxy("http://user:p@ss@proxy.example:3128")
    assert (proxy.username, proxy.password, proxy.host) == ("user", "p@ss", "proxy.example")


def test_login_without_a_password_and_a_login_with_special_characters() -> None:
    assert parse_proxy("http://just_login@proxy.example:3128").password == ""
    assert parse_proxy("http://a%40b.ru:x@proxy.example:3128").username == "a@b.ru"


def test_ipv6_host_goes_to_the_flag_in_brackets() -> None:
    proxy = parse_proxy("http://[2001:db8::1]:3128")
    assert proxy.host == "2001:db8::1"
    assert proxy.server == "http://[2001:db8::1]:3128"
    assert proxy.masked() == "http://[2001:db8::1]:3128"


def test_socks_with_a_password_is_refused() -> None:
    with pytest.raises(ValueError, match="SOCKS"):
        parse_proxy("socks5://user:secret@10.0.0.1:1080")


@pytest.mark.parametrize("text", ["", "http://", "http://host", "http://host:port", "ftp://host:21", "http://:3128"])
def test_malformed_proxies_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_proxy(text)


def test_refusals_never_repeat_the_password() -> None:
    for text in ("ftp://user:TOPSECRET@host:21", "http://user:TOPSECRET@host:port", "socks5://user:TOPSECRET@h:1"):
        with pytest.raises(ValueError) as error:
            parse_proxy(text)
        assert "TOPSECRET" not in str(error.value)


def test_password_is_masked_in_text_and_repr() -> None:
    proxy = parse_proxy(PROXY_URL)
    assert proxy.masked() == "http://user:***@proxy.example:3128"
    assert PASSWORD not in proxy.masked() and ENCODED not in proxy.masked()
    assert PASSWORD not in repr(proxy) and "p%40ss" not in repr(proxy)


@pytest.mark.parametrize(
    ("text", "expected"), [("ru", "RU"), (" Kz ", "KZ"), ("off", "OFF"), ("NO", "OFF"), ("0", "OFF")]
)
def test_country_codes(text: str, expected: str) -> None:
    assert normalize_country(text) == expected


@pytest.mark.parametrize("text", ["", "RUS", "1", "R1"])
def test_wrong_country_codes_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        normalize_country(text)


def test_ip_answers_of_the_services() -> None:
    assert extract_ip_info('{"ip": "1.2.3.4", "country": "ru"}', "country", "ip") == ("1.2.3.4", "RU")
    assert extract_ip_info('{"ip": "1.2.3.4", "country_code": "DE"}', "country_code", "ip") == ("1.2.3.4", "DE")
    assert extract_ip_info("<html>captive portal</html>", "country", "ip") == (None, None)
    assert extract_ip_info("[1, 2]", "country", "ip") == (None, None)
    assert extract_ip_info('{"ip": "1.2.3.4"}', "country", "ip") == ("1.2.3.4", None)


# ---------- settings ----------


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_defaults_of_the_new_settings() -> None:
    settings = _settings()
    assert settings.TWOGIS_PROXY is None
    assert settings.TWOGIS_PROXY_COUNTRY == "RU"
    assert settings.TWOGIS_PROFILE_DIR == "data/cdp_profile"
    assert settings.TWOGIS_TIMEZONE == "Europe/Moscow"
    assert settings.TWOGIS_LANG == "ru-RU"
    assert settings.TWOGIS_HEADLESS is False


def test_the_proxy_password_stays_out_of_repr_and_str_of_the_settings() -> None:
    settings = _settings(TWOGIS_PROXY=PROXY_URL)
    assert settings.TWOGIS_PROXY is not None
    assert settings.TWOGIS_PROXY.get_secret_value() == PROXY_URL
    for text in (repr(settings), str(settings), repr(settings.TWOGIS_PROXY), repr(CdpOptions.from_settings(settings))):
        assert PASSWORD not in text and ENCODED not in text


def test_blank_values_mean_the_defaults() -> None:
    settings = _settings(TWOGIS_PROXY="  ", TWOGIS_PROXY_COUNTRY="", TWOGIS_TIMEZONE="", TWOGIS_LANG="")
    assert settings.TWOGIS_PROXY is None
    assert (settings.TWOGIS_PROXY_COUNTRY, settings.TWOGIS_TIMEZONE, settings.TWOGIS_LANG) == (
        "RU",
        "Europe/Moscow",
        "ru-RU",
    )


def test_country_check_can_be_turned_off() -> None:
    assert _settings(TWOGIS_PROXY_COUNTRY="off").TWOGIS_PROXY_COUNTRY == "OFF"
    assert ChromeOptions(proxy_country="off").country_to_check is None
    assert ChromeOptions(proxy_country="ru").country_to_check == "RU"


@pytest.mark.parametrize(
    "bad",
    ["ftp://user:TOPSECRET@host:21", "http://user:TOPSECRET@host:nope", "socks5://user:TOPSECRET@10.0.0.1:1080"],
)
def test_a_bad_proxy_stops_the_start_and_never_prints_the_password(bad: str) -> None:
    with pytest.raises(ProxySettingsError) as error:
        _settings(TWOGIS_PROXY=bad)
    assert "TWOGIS_PROXY" in str(error.value)
    assert "TOPSECRET" not in str(error.value)


def test_a_bad_proxy_in_the_env_file_does_not_leak_either(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TWOGIS_PROXY=ftp://user:TOPSECRET@host:21\n", encoding="utf-8")
    with pytest.raises(ProxySettingsError) as error:
        Settings(_env_file=env)
    assert "TOPSECRET" not in str(error.value) and "TOPSECRET" not in repr(error.value)


@pytest.mark.parametrize(
    "values",
    [{"TWOGIS_PROXY_COUNTRY": "RUS"}, {"TWOGIS_TIMEZONE": "Mars/Base"}, {"TWOGIS_LANG": "русский"}],
)
def test_bad_values_are_refused(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_the_old_name_of_the_profile_folder_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPER_CDP_PROFILE_DIR", "data/old_profile")
    assert Settings(_env_file=None).TWOGIS_PROFILE_DIR == "data/old_profile"
    monkeypatch.setenv("TWOGIS_PROFILE_DIR", "data/new_profile")
    assert Settings(_env_file=None).TWOGIS_PROFILE_DIR == "data/new_profile"


def test_an_old_env_file_with_the_api_key_still_loads(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "BOT_TOKEN=123:abc\nADMIN_TG_ID=5\nDGIS_API_KEY=oldkey\nTWOGIS_API_KEY=oldkey2\nDGIS_DEMO_KEY=true\n"
        "DGIS_REQUEST_CONTACTS=false\nDGIS_REQUEST_LIMIT=1000\nDGIS_REQUEST_RESERVE=50\n"
        "DGIS_KEY_CREATED_AT=2026-11-01\nDGIS_KEY_WARN_DAYS=5\nCOLLECT_PROVIDERS=2gis,osm\nCOLLECTOR_PROVIDERS=2gis\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)
    assert settings.ADMIN_TG_ID == 5
    assert not hasattr(settings, "DGIS_API_KEY") and not hasattr(settings, "TWOGIS_API_KEY")
    assert load_collector_config(settings).providers == ["2gis_cdp", "osm"]


def test_the_old_provider_name_in_the_collector_setting_is_an_alias(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(COLLECTOR_PROVIDERS="2gis")
    with caplog.at_level("WARNING"):
        config = collector_cli._collector_config(settings)
    assert config.providers == ["2gis_cdp"]
    assert "COLLECT_PROVIDERS" in caplog.text  # the collector config is built from the same loader


def test_secrets_are_hidden_in_what_the_collector_prints() -> None:
    settings = _settings(BOT_TOKEN="123456:BOTSECRET", TWOGIS_PROXY=PROXY_URL)
    text = collector_cli._without_secrets(f"error through {PROXY_URL} and 123456:BOTSECRET", settings)
    assert PASSWORD not in text and ENCODED not in text and "BOTSECRET" not in text


# ---------- the command line of Chrome ----------


def _spawned_cmd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **options: Any) -> tuple[ChromeBrowser, list[str]]:
    fake_bin = tmp_path / "chrome"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    spawned: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        spawned.append(cmd)
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    browser = ChromeBrowser(ChromeOptions(binary_path=fake_bin, profile_dir=tmp_path / "profile", **options))
    return browser, spawned[0]


def test_chrome_gets_the_proxy_without_its_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    browser, cmd = _spawned_cmd(monkeypatch, tmp_path, proxy=SecretStr(PROXY_URL), lang="ru-RU")
    assert "--proxy-server=http://proxy.example:3128" in cmd
    assert "--proxy-bypass-list=<-loopback>" in cmd
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in cmd
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in cmd
    assert "--lang=ru-RU" in cmd and "--accept-lang=ru-RU" in cmd
    assert not any(PASSWORD in arg or ENCODED in arg or "user:" in arg for arg in cmd)
    assert PASSWORD not in repr(browser) and ENCODED not in repr(browser)


def test_chrome_without_a_proxy_and_with_the_profile_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, cmd = _spawned_cmd(monkeypatch, tmp_path)
    assert not any(arg.startswith(("--proxy", "--lang", "--headless", "--force-webrtc")) for arg in cmd)
    assert f"--user-data-dir={tmp_path / 'profile'}" in cmd
    assert (tmp_path / "profile").is_dir()


def test_headless_is_an_option_and_the_window_is_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, cmd = _spawned_cmd(monkeypatch, tmp_path, headless=True)
    assert "--headless=new" in cmd and "--window-size=1920,1080" in cmd


def test_options_keep_the_password_out_of_repr_and_of_validation_errors() -> None:
    options = ChromeOptions(proxy=SecretStr(PROXY_URL))
    assert PASSWORD not in repr(options) and ENCODED not in repr(options)
    with pytest.raises(ValidationError) as error:
        ChromeOptions(proxy="ftp://user:TOPSECRET@host:21")
    assert "TOPSECRET" not in str(error.value)


# ---------- the answer to the login request of the proxy ----------


def _remote_with_tab(**options: Any) -> tuple[ChromeRemote, MagicMock]:
    remote = ChromeRemote(ChromeOptions(**options), [])
    tab = MagicMock()
    remote._chrome_tab = tab
    remote._setup_tab()
    return remote, tab


def _challenge(source: str = "Proxy") -> dict[str, Any]:
    return {"requestId": "r1", "authChallenge": {"source": source}}


def test_the_login_is_given_over_cdp_to_the_proxy_only() -> None:
    _, tab = _remote_with_tab(proxy=SecretStr(PROXY_URL))
    tab.Fetch.enable.assert_called_once_with(handleAuthRequests=True, patterns=[{"urlPattern": "*"}])

    tab.Fetch.authRequired(**_challenge("Proxy"))
    tab.Fetch.continueWithAuth.assert_called_with(
        requestId="r1",
        authChallengeResponse={"response": "ProvideCredentials", "username": "user", "password": PASSWORD},
    )
    tab.Fetch.authRequired(**_challenge("Server"))  # a site asking for a login never gets the one of the proxy
    assert tab.Fetch.continueWithAuth.call_args.kwargs["authChallengeResponse"] == {"response": "Default"}

    tab.Fetch.requestPaused(requestId="r2")
    tab.Fetch.continueRequest.assert_called_with(requestId="r2")


def test_endless_requests_for_the_login_end_in_a_refusal() -> None:
    remote, tab = _remote_with_tab(proxy=SecretStr(PROXY_URL))
    for _ in range(_PROXY_AUTH_MAX_ATTEMPTS):
        tab.Fetch.authRequired(**_challenge())
    assert not remote._proxy_auth_failed
    tab.Fetch.authRequired(**_challenge())
    assert remote._proxy_auth_failed
    assert tab.Fetch.continueWithAuth.call_args.kwargs["authChallengeResponse"] == {"response": "CancelAuth"}


def test_nothing_is_intercepted_without_a_login() -> None:
    _, tab = _remote_with_tab(proxy=SecretStr("http://proxy.example:3128"))
    tab.Fetch.enable.assert_not_called()
    _, tab = _remote_with_tab()
    tab.Fetch.enable.assert_not_called()


def test_timezone_follows_the_proxy_and_the_headless_window_gets_a_size() -> None:
    _, tab = _remote_with_tab(timezone="Europe/Moscow", headless=True)
    tab.Emulation.setTimezoneOverride.assert_called_once_with(timezoneId="Europe/Moscow")
    tab.Emulation.setDeviceMetricsOverride.assert_called_once()
    tab.Network.setUserAgentOverride.assert_not_called()  # the user agent stays what the browser says
    _, tab = _remote_with_tab()
    tab.Emulation.setTimezoneOverride.assert_not_called()
    tab.Emulation.setDeviceMetricsOverride.assert_not_called()


# ---------- the check of the exit IP ----------


class IpServices:
    """What the three IP services answer in the browser, in order; an Exception is a failed navigation."""

    def __init__(self, answers: list[str | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self._current = ""

    def attach(self, remote: ChromeRemote) -> None:
        remote.navigate = self._navigate  # type: ignore[method-assign]
        remote.execute_script = lambda _expression: self._current  # type: ignore[method-assign]
        remote._wait_document_ready = lambda: True  # type: ignore[method-assign]
        remote.clear_requests = MagicMock()  # type: ignore[method-assign]

    def _navigate(self, url: str, timeout: int = 60) -> None:
        self.asked.append(url)
        answer = self.answers[len(self.asked) - 1]
        if isinstance(answer, Exception):
            from src.services.scraper.chrome.exceptions import ChromeException

            raise ChromeException(str(answer))
        self._current = answer


def _checked(answers: list[str | Exception], **options: Any) -> tuple[ChromeRemote, IpServices]:
    remote = ChromeRemote(ChromeOptions(**options), [])
    services = IpServices(answers)
    services.attach(remote)
    return remote, services


RU_ANSWER = '{"ip": "203.0.113.7", "country": "RU"}'


def test_the_right_country_passes_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    remote, services = _checked([RU_ANSWER], proxy=SecretStr(PROXY_URL), proxy_country="RU")
    with caplog.at_level("INFO"):
        remote._check_exit_ip()
    assert (remote.exit_ip, remote.exit_country) == ("203.0.113.7", "RU")
    assert "External IP: 203.0.113.7 (RU)" in caplog.text
    assert PASSWORD not in caplog.text and ENCODED not in caplog.text
    assert len(services.asked) == 1
    remote.clear_requests.assert_called_once()  # type: ignore[attr-defined]


def test_the_next_service_is_asked_when_the_first_one_fails() -> None:
    remote, services = _checked(
        [RuntimeError("boom"), "<html>not json</html>", '{"ip": "203.0.113.7", "country": "RU"}'],
        proxy=SecretStr(PROXY_URL),
        proxy_country="RU",
    )
    remote._check_exit_ip()
    assert len(services.asked) == 3 and remote.exit_country == "RU"


def test_a_foreign_ip_stops_the_run() -> None:
    remote, _ = _checked(['{"ip": "198.51.100.9", "country": "DE"}'], proxy=SecretStr(PROXY_URL), proxy_country="RU")
    with pytest.raises(ProxyError) as error:
        remote._check_exit_ip()
    assert "DE" in str(error.value) and "RU" in str(error.value)
    assert PASSWORD not in str(error.value) and ENCODED not in str(error.value)


def test_a_dead_proxy_stops_the_run_and_the_message_hides_the_password() -> None:
    failures: list[str | Exception] = [RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")] * 3
    remote, _ = _checked(failures, proxy=SecretStr(PROXY_URL), proxy_country="RU")
    with pytest.raises(ProxyError) as error:
        remote._check_exit_ip()
    assert "ERR_PROXY_CONNECTION_FAILED" in str(error.value)
    assert "user:***@proxy.example:3128" in str(error.value)
    assert PASSWORD not in str(error.value) and ENCODED not in str(error.value)


def test_a_refused_login_stops_the_run() -> None:
    remote, services = _checked([RuntimeError("net::ERR_ABORTED")] * 3, proxy=SecretStr(PROXY_URL), proxy_country="RU")
    remote._proxy_auth_failed = True
    with pytest.raises(ProxyError, match="не принял логин"):
        remote._check_exit_ip()
    assert len(services.asked) == 1  # no sense in asking the other services


def test_without_a_proxy_the_country_is_checked_too_and_unknown_is_a_stop() -> None:
    remote, _ = _checked(['{"ip": "198.51.100.9", "country": "FI"}'], proxy_country="RU")
    with pytest.raises(ProxyError, match="FI"):
        remote._check_exit_ip()
    remote, _ = _checked([RuntimeError("offline")] * 3, proxy_country="RU")
    with pytest.raises(ProxyError, match="TWOGIS_PROXY_COUNTRY=off"):
        remote._check_exit_ip()


def test_with_the_check_off_any_country_goes_through() -> None:
    remote, _ = _checked(['{"ip": "198.51.100.9", "country": "DE"}'], proxy=SecretStr(PROXY_URL), proxy_country="off")
    remote._check_exit_ip()
    assert remote.exit_country == "DE"


def _started_remote(monkeypatch: pytest.MonkeyPatch, **options: Any) -> tuple[ChromeRemote, list[str]]:
    """start() with the parts that need a real Chrome replaced: only the order of the steps matters here."""
    steps: list[str] = []
    remote = ChromeRemote(ChromeOptions(**options), [])
    monkeypatch.setattr("src.services.scraper.chrome.remote.ChromeBrowser", lambda _o: MagicMock(remote_port=9333))
    monkeypatch.setattr("src.services.scraper.chrome.remote.patch_pychrome", lambda: None)
    monkeypatch.setattr(remote, "_connect_interface", lambda: steps.append("connect"))
    monkeypatch.setattr(remote, "_setup_tab", lambda: steps.append("setup"))
    monkeypatch.setattr(remote, "_init_tab_monitor", lambda: steps.append("monitor"))
    monkeypatch.setattr(remote, "_check_exit_ip", lambda: steps.append("check"))
    monkeypatch.setattr(remote, "stop", lambda: steps.append("stop"))
    return remote, steps


def test_the_ip_is_checked_at_the_start_before_anything_else_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    remote, steps = _started_remote(monkeypatch, proxy=SecretStr(PROXY_URL), proxy_country="RU")
    remote.start()
    assert steps == ["connect", "setup", "monitor", "check"]


def test_a_failed_check_closes_the_browser_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    remote, steps = _started_remote(monkeypatch, proxy=SecretStr(PROXY_URL), proxy_country="RU")
    monkeypatch.setattr(remote, "_check_exit_ip", lambda: (_ for _ in ()).throw(ProxyError("wrong country")))
    with pytest.raises(ProxyError):
        remote.start()
    assert steps[-1] == "stop"


def test_no_check_without_a_proxy_and_with_the_check_off(monkeypatch: pytest.MonkeyPatch) -> None:
    remote, steps = _started_remote(monkeypatch, proxy_country="off")
    remote.start()
    assert "check" not in steps


# ---------- ProxyError stops the run and goes to the admins ----------


class FailingSession:
    """A browser session whose start finds that the proxy does not work."""

    def __init__(self) -> None:
        self.starts = 0
        self.closed = 0

    async def start(self) -> None:
        self.starts += 1
        raise ProviderProxyError("Внешний IP 198.51.100.9 из страны DE, а нужна RU")

    async def read_search(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nothing may be read through a wrong proxy")

    async def close(self) -> None:
        self.closed += 1


async def test_chrome_proxy_error_becomes_a_provider_error_of_its_own() -> None:
    class BadRemote:
        def start(self) -> None:
            raise ProxyError("прокси не отвечает")

        def stop(self) -> None:
            return None

    session = ChromeScraperSession(gate=lambda _action: None, remote_factory=lambda _o, _p: BadRemote())  # type: ignore[arg-type,return-value]
    with pytest.raises(ProviderProxyError, match="прокси не отвечает"):
        await session.start()


async def test_the_provider_stops_the_run_before_robots_and_pages() -> None:
    session = FailingSession()
    robots_loaded: list[bool] = []

    async def load_rules() -> Any:
        robots_loaded.append(True)
        raise AssertionError("robots.txt of 2GIS must not be asked from a wrong IP")

    provider = TwoGisCdpProvider(CdpOptions(), session_factory=lambda _gate: session, robots_loader=load_rules)  # type: ignore[arg-type,return-value]
    with pytest.raises(ProviderProxyError):
        await provider.search(QUERY, AREA)
    with pytest.raises(ProviderProxyError):  # the next target of the same run does not even try
        await provider.search(QUERY, AREA)
    assert session.starts == 1 and robots_loaded == []
    assert not provider.run_counted  # nothing of 2GIS was visited: the interval is not spent

    await provider.close()
    assert session.closed == 1


class ProxyFailingProvider(BaseMapProvider):
    """Stands in for 2gis_cdp: the proxy does not work."""

    name = "2gis_cdp"
    min_hours_between_runs = 20.0

    def __init__(self) -> None:
        self.calls = 0
        self.run_counted = False

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query: Any, area: Any, page: int = 1, page_size: int = 50) -> SearchPage:
        self.calls += 1
        raise ProviderProxyError(
            "Внешний IP 198.51.100.9 из страны DE, а нужна RU (http://user:***@proxy.example:3128)"
        )


class WorkingProvider(BaseMapProvider):
    name = "osm"

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query: Any, area: Any, page: int = 1, page_size: int = 50) -> SearchPage:
        return SearchPage(
            places=[
                RawPlace(
                    external_id="osm_1",
                    source="osm",
                    name="Организация",
                    category="dentist_clinic",
                    phone="+7 812 555-01-01",
                )
            ]
        )


class Reports:
    def __init__(self) -> None:
        self.items: list[RunReport] = []

    async def __call__(self, report: RunReport) -> None:
        self.items.append(report)


@pytest.fixture
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url: str, client: Any = None) -> tuple[str, None]:
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


async def test_proxy_error_stops_the_source_for_the_run_without_a_block(
    session_maker: async_sessionmaker[AsyncSession], _no_site_checks: None
) -> None:
    failing, working = ProxyFailingProvider(), WorkingProvider()
    config = load_collector_config(
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, COLLECT_QUERIES="dentist_clinic,beauty")
    )
    reports = Reports()
    runner = CollectorRunner(session_maker, ProviderSet(providers=[failing, working]), config, reports)

    await runner.run_once(TRIGGER_MANUAL)

    report = reports.items[0]
    assert failing.calls == 1  # the second target is not tried: the source is out for the run
    assert len(report.new_alerts) == 1
    alert = report.new_alerts[0]
    assert "Прокси" in alert and "DE" in alert and "не блокировка" in alert
    assert report.result.saved_new == 1  # the other source went on

    async with session_maker() as session:
        states = await get_provider_states(session)
    state = states.get("2gis_cdp")
    assert state is None or (
        state.disabled_at is None and state.blocked_until is None and state.consecutive_blocks == 0
    )
    assert state is None or state.last_run_at is None  # the run was not spent: the next try is not postponed


async def test_the_alert_about_the_proxy_reaches_the_admins() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    notifier = AdminNotifier(bot, [1], [], True)
    report = MagicMock(new_alerts=["🌐 Прокси для 2gis_cdp: Внешний IP из страны DE"], announce=False)

    await notifier.report(report)

    texts = [call.kwargs.get("text") or call.args[1] for call in bot.send_message.await_args_list]
    assert any("Прокси для 2gis_cdp" in str(text) for text in texts)


# ---------- robots.txt goes out the way the browser does ----------


async def test_robots_are_fetched_through_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, timeout: float) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(robots.httpx, "AsyncClient", FakeClient)
    provider = TwoGisCdpProvider(CdpOptions(proxy=SecretStr(PROXY_URL)))
    rules = await provider._fetch_robots()
    assert created[0]["proxy"] == PROXY_URL
    assert not rules.is_allowed("https://2gis.ru/private")

    created.clear()
    await TwoGisCdpProvider(CdpOptions())._fetch_robots()
    assert created[0]["proxy"] is None


async def test_the_options_of_the_provider_come_from_the_settings() -> None:
    options = CdpOptions.from_settings(
        _settings(
            TWOGIS_PROXY=PROXY_URL,
            TWOGIS_PROXY_COUNTRY="kz",
            TWOGIS_TIMEZONE="Asia/Almaty",
            TWOGIS_LANG="kk-KZ",
            TWOGIS_HEADLESS="true",
        )
    )
    assert options.proxy is not None and options.proxy.get_secret_value() == PROXY_URL
    assert (options.proxy_country, options.timezone, options.lang, options.headless) == (
        "KZ",
        "Asia/Almaty",
        "kk-KZ",
        True,
    )
    assert PASSWORD not in repr(options)


async def test_the_session_passes_the_options_to_chrome() -> None:
    options = CdpOptions(proxy=SecretStr(PROXY_URL), proxy_country="RU", timezone="Europe/Moscow", lang="ru-RU")
    created: list[Any] = []

    def factory(chrome_options: Any, _patterns: list[str]) -> Any:
        created.append(chrome_options)
        raise ProxyError("stop here")

    provider = TwoGisCdpProvider(CdpOptions(**options.__dict__))
    session = provider._chrome_session(lambda _action: None)  # type: ignore[arg-type,return-value]
    session._remote_factory = factory  # type: ignore[attr-defined]
    with pytest.raises(ProviderProxyError):
        await session.start()
    chrome_options = created[0]
    assert chrome_options.proxy.get_secret_value() == PROXY_URL
    assert (chrome_options.proxy_country, chrome_options.timezone, chrome_options.lang) == (
        "RU",
        "Europe/Moscow",
        "ru-RU",
    )
    assert asyncio.iscoroutinefunction(session.start)
