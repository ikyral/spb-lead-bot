"""Exceptions raised by the Chrome DevTools Protocol browser layer."""


class ChromeException(Exception):
    """Base failure in launching or driving Chrome/Chromium over CDP."""


class ChromePathNotFound(ChromeException):
    """Chrome/Chromium executable could not be found on this machine."""

    def __init__(self, message: str = "Chrome/Chromium браузер не найден") -> None:
        super().__init__(message)


class ChromeUserAbortException(ChromeException):
    """CDP tab connection was closed deliberately."""


class ChromeRuntimeException(ChromeException):
    """Chrome tab crashed, detached, or rejected a CDP command."""
