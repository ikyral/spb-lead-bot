"""Cross-platform utilities for locating Chrome, reserving a port, and polling CDP state."""

from __future__ import annotations

import functools
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

_LINUX_APP_DIRS: tuple[str, ...] = (
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/sbin",
    "/opt/google/chrome",
)
_LINUX_EXECUTABLES: tuple[str, ...] = (
    "google-chrome",
    "chrome",
    "chrome-browser",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def floor_to_hundreds(value: int | float) -> int:
    """Round number down to the nearest multiple of 100."""
    return int(value // 100 * 100)


def wait_until_finished(
    timeout: float | None = None,
    finished: Callable[[Any], bool] = lambda value: bool(value),
    throw_exception: bool = True,
    poll_interval: float = 0.1,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Poll wrapped synchronous function until `finished(ret)` returns True or `timeout` expires."""

    def outer(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def inner(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            call_time = time.monotonic()
            while True:
                ret = func(*args, **kwargs)
                if finished(ret):
                    return ret
                if timeout is not None and (time.monotonic() - call_time) > timeout:
                    if throw_exception:
                        raise TimeoutError(f"{func.__name__} timed out after {timeout}s")
                    return ret
                time.sleep(poll_interval)

        return inner

    return outer


def _playwright_cached_chrome() -> str | None:
    """Fallback to Playwright's downloaded Chromium binary if system Chrome is absent."""
    cache_root = Path.home() / ".cache" / "ms-playwright"
    if sys.platform == "darwin":
        cache_root = Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        cache_root = (Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local") / "ms-playwright"

    if not cache_root.is_dir():
        return None
    patterns = (
        ("chromium-*", "chrome-win64", "chrome.exe")
        if sys.platform == "win32"
        else (
            "chromium-*",
            "chrome-mac*",
            "Google Chrome for Testing.app",
            "Contents",
            "MacOS",
            "Google Chrome for Testing",
        )
        if sys.platform == "darwin"
        else ("chromium-*", "chrome-linux*", "chrome")
    )
    glob_pattern = "/".join(patterns)
    for candidate in sorted(cache_root.glob(glob_pattern), reverse=True):
        if candidate.is_file():
            return str(candidate)
    return None


def locate_chrome_path() -> str | None:
    """Locate Google Chrome (or Chromium fallback) executable path on the current OS."""
    if sys.platform == "win32":
        import ctypes
        import winreg

        app_dirs: list[str] = []
        csidl = {
            "CSIDL_PROGRAM_FILES": 38,
            "CSIDL_PROGRAM_FILESX86": 42,
            "CSIDL_LOCAL_APPDATA": 28,
            "CSIDL_COMMON_APPDATA": 35,
            "CSIDL_APPDATA": 26,
        }
        shell32 = ctypes.windll.shell32
        for folder_id in csidl.values():
            buf = ctypes.create_unicode_buffer(1024)
            shell32.SHGetFolderPathW(None, folder_id, None, 0, buf)
            if buf.value:
                app_dirs.append(buf.value)

        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA"):
            env_val = os.environ.get(env_name)
            if env_val and env_val not in app_dirs:
                app_dirs.append(env_val)

        for base_dir in app_dirs:
            binary_path = os.path.join(base_dir, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(binary_path):
                return binary_path

        reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        for install_type in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(install_type, reg_path, 0, winreg.KEY_READ) as reg_key:
                    value = winreg.QueryValue(reg_key, None)
                    if value and os.path.isfile(value):
                        return str(value)
            except OSError:
                continue

    elif sys.platform == "darwin":
        for binary_path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ):
            if os.path.isfile(binary_path):
                return binary_path

    else:
        for base_dir in _LINUX_APP_DIRS:
            for name in _LINUX_EXECUTABLES:
                binary_path = os.path.join(base_dir, name)
                if os.path.isfile(binary_path):
                    return binary_path

        for name in _LINUX_EXECUTABLES:
            found = shutil.which(name)
            if found and os.path.isfile(found):
                return found

    return _playwright_cached_chrome()


def free_port() -> int:
    """Reserve and return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free_socket:
        free_socket.bind(("127.0.0.1", 0))
        free_socket.listen(5)
        return int(free_socket.getsockname()[1])
