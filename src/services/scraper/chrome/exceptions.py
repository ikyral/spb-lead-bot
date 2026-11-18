"""Exceptions raised by the Chrome DevTools Protocol browser layer."""


class ChromeException(Exception):
    """Base failure in launching or driving Chrome/Chromium over CDP."""


class ChromePathNotFound(ChromeException):
    """Chrome/Chromium executable could not be found on this machine."""

    def __init__(self, message: str = "Chrome/Chromium браузер не найден") -> None:
        super().__init__(message)


class ProxyError(ChromeException):
    """
    The proxy does not answer or does not accept the login, or the exit IP is from the wrong country.

    Fatal for the whole run: going on would reach 2GIS from an IP that was not meant for it. This is a
    problem of this side's network, not a refusal of the site.
    """


class ChromeUserAbortException(ChromeException):
    """CDP tab connection was closed deliberately."""


class ChromeRuntimeException(ChromeException):
    """Chrome tab crashed, detached, or rejected a CDP command."""
