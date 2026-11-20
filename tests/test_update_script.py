"""Tests for deploy/spb-lead-bot-update.sh: no network, no real systemd, local bare git "origin"."""

import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "spb-lead-bot-update.sh"
GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sqlite3") is None or shutil.which("git") is None,
    reason="bash, sqlite3 and git are required",
)

_RECORD_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    result = subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%d %H:%M:%S.%f")


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "app"
        self.origin = root / "origin.git"
        self.db = root / "leads.db"
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.bin.mkdir()
        self.log.touch()
        for name in ("sudo", "systemctl"):
            stub = self.bin / name
            stub.write_text(_RECORD_STUB)
            stub.chmod(0o755)
        if shutil.which("flock") is None:  # e.g. macOS: the lock itself is not under test
            (self.bin / "flock").write_text("#!/bin/sh\nexit 0\n")
            (self.bin / "flock").chmod(0o755)
        if shutil.which("sha256sum") is None:
            (self.bin / "sha256sum").write_text('#!/bin/sh\nshasum -a 256 "$@"\n')
            (self.bin / "sha256sum").chmod(0o755)

        _git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(root, "clone", "-q", str(self.origin), str(self.app))
        (self.app / "requirements.txt").write_text("httpx\n")
        _git(self.app, "add", ".")
        _git(self.app, "commit", "-q", "-m", "first")
        _git(self.app, "push", "-q", "origin", "HEAD:main")

        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE collector_state (id INTEGER PRIMARY KEY, last_status TEXT, "
            "last_started_at DATETIME, last_finished_at DATETIME)"
        )
        con.commit()
        con.close()

    def set_state(self, status: str | None, started: str | None, finished: str | None) -> None:
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM collector_state")
        con.execute("INSERT INTO collector_state VALUES (1, ?, ?, ?)", (status, started, finished))
        con.commit()
        con.close()

    def push_new_commit(self) -> None:
        other = self.root / "other"
        _git(self.root, "clone", "-q", str(self.origin), str(other))
        (other / "new.txt").write_text("x\n")
        _git(other, "add", ".")
        _git(other, "commit", "-q", "-m", "second")
        _git(other, "push", "-q", "origin", "HEAD:main")

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "APP_DIR": str(self.app),
            "LOCK_FILE": str(self.root / "update.lock"),
            "SETTLE_SECONDS": "0",
            "UPDATE_DB_PATH": str(self.db),
            "STUB_LOG": str(self.log),
            **extra,
        }
        return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def calls(self) -> str:
        return self.log.read_text()

    def head(self) -> str:
        return _git(self.app, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_collection_running_postpones_update(env: Env) -> None:
    env.push_new_commit()
    before = env.head()
    env.set_state("running", _ts(timedelta(minutes=20)), None)

    result = env.run()

    assert result.returncode == 0
    assert "collection in progress, update postponed" in result.stdout
    assert env.head() == before
    assert "restart" not in env.calls()


def test_finished_collection_allows_update(env: Env) -> None:
    env.push_new_commit()
    before = env.head()
    env.set_state("ok", _ts(timedelta(minutes=40)), _ts(timedelta(minutes=5)))

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert env.head() != before
    assert "restart spb-lead-bot" in env.calls()


def test_interrupted_status_allows_update(env: Env) -> None:
    # The app marks a run killed by a restart as "interrupted" at the next start.
    env.push_new_commit()
    env.set_state("interrupted", _ts(timedelta(minutes=10)), None)

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert "restart spb-lead-bot" in env.calls()


def test_stale_running_marker_is_ignored(env: Env) -> None:
    env.push_new_commit()
    before = env.head()
    env.set_state("running", _ts(timedelta(hours=4)), None)

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert env.head() != before
    assert "restart spb-lead-bot" in env.calls()


def test_missing_sqlite_fails_open(env: Env) -> None:
    env.push_new_commit()
    env.set_state("running", _ts(timedelta(minutes=5)), None)

    result = env.run(UPDATE_SQLITE_BIN="sqlite3-does-not-exist")

    assert result.returncode == 0, result.stderr
    assert "guard skipped" in result.stderr
    assert "restart spb-lead-bot" in env.calls()


def test_unreadable_db_fails_open(env: Env) -> None:
    env.push_new_commit()

    result = env.run(UPDATE_DB_PATH=str(env.root / "missing.db"))

    assert result.returncode == 0, result.stderr
    assert "guard skipped" in result.stderr
    assert "restart spb-lead-bot" in env.calls()


def test_broken_db_fails_open(env: Env) -> None:
    env.push_new_commit()
    env.db.write_text("this is not a database")

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert "guard skipped" in result.stderr
    assert "restart spb-lead-bot" in env.calls()


def test_nothing_new_does_not_restart(env: Env) -> None:
    env.set_state("ok", _ts(timedelta(hours=1)), _ts(timedelta(minutes=30)))

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert "restart" not in env.calls()
