"""
Chrome process management over temporary or persistent user data directories.

The browser starts with its own defaults and a visible window: nothing here changes how it
presents itself to sites or hides that it is driven over the DevTools protocol.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

from .exceptions import ChromePathNotFound
from .utils import free_port, locate_chrome_path, wait_until_finished

if TYPE_CHECKING:
    from .options import ChromeOptions

_TERMINATE_TIMEOUT_SECONDS: int = 5


class ChromeBrowser:
    """Launch Chrome/Chromium with remote debugging enabled."""

    def __init__(self, chrome_options: ChromeOptions) -> None:
        binary_path = str(chrome_options.binary_path) if chrome_options.binary_path else locate_chrome_path()
        if not binary_path or not os.path.isfile(binary_path):
            raise ChromePathNotFound(
                "браузер Chrome/Chromium не найден. Установите Google Chrome или укажите SCRAPER_CDP_CHROME_PATH"
            )

        self._owns_profile = chrome_options.profile_dir is None
        if chrome_options.profile_dir is not None:
            chrome_options.profile_dir.mkdir(parents=True, exist_ok=True)
            self._profile_path = str(chrome_options.profile_dir)
        else:
            self._profile_path = tempfile.mkdtemp(prefix="spb_2gis_")

        # The debugging port listens on 127.0.0.1 only, and only a page of that origin may connect to it.
        self._remote_port = free_port()
        self._chrome_cmd: list[str] = [
            binary_path,
            f"--remote-debugging-port={self._remote_port}",
            f"--user-data-dir={self._profile_path}",
            "--no-default-browser-check",
            "--no-first-run",
            f"--remote-allow-origins=http://127.0.0.1:{self._remote_port}",
            f"--js-flags=--expose-gc --max-old-space-size={chrome_options.memory_limit}",
        ]

        if chrome_options.start_maximized:
            self._chrome_cmd.append("--start-maximized")

        if chrome_options.silent_browser:
            # Launches verified browser binary without a shell.
            self._proc = subprocess.Popen(  # noqa: S603
                self._chrome_cmd,
                shell=False,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        else:
            # Launches verified browser binary without a shell.
            self._proc = subprocess.Popen(self._chrome_cmd, shell=False)  # noqa: S603

    @property
    def remote_port(self) -> int:
        """Remote debugging port."""
        return self._remote_port

    @property
    def profile_path(self) -> str:
        """Path to `--user-data-dir` passed to Chrome."""
        return self._profile_path

    @property
    def returncode(self) -> int | None:
        """Exit code if Chrome already exited, else None."""
        return self._proc.poll()

    @wait_until_finished(timeout=5, throw_exception=False)
    def _delete_profile(self) -> bool:
        """Remove temporary profile directory created for this run."""
        shutil.rmtree(self._profile_path, ignore_errors=True)
        return not os.path.isdir(self._profile_path)

    def close(self) -> None:
        """Terminate Chrome process and delete temporary profile if we created one."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)

        if self._owns_profile:
            self._delete_profile()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(arguments={self._chrome_cmd!r})"
