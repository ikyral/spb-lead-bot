"""Tests for deploy/backup-db.sh: no network, no real systemd, fake curl in PATH."""

import os
import shutil
import sqlite3
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "backup-db.sh"
BASH = shutil.which("bash") or "bash"
TOKEN = "123456:SECRET-token_value"  # noqa: S105 (fake test value)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sqlite3") is None,
    reason="bash and sqlite3 are required",
)

_CURL_STUB = (
    "#!/bin/sh\n"
    'echo call >> "$STUB_DIR/calls"\n'
    'n=$(wc -l < "$STUB_DIR/calls" | tr -d " ")\n'
    'cat > "$STUB_DIR/stdin_$n"\n'
    'exit "${CURL_EXIT:-0}"\n'
)


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "leads.db"
        self.dest = root / "backups" / "daily"
        self.stub = root / "stub"
        self.stub.mkdir()
        (self.stub / "calls").touch()
        curl = self.stub / "curl"
        curl.write_text(_CURL_STUB)
        curl.chmod(0o755)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO leads (name) VALUES ('a'), ('b')")
        con.commit()
        con.close()

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.stub}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.stub),
            "DB": str(self.db),
            "DEST": str(self.dest),
            "KEEP": "7",
            "BOT_TOKEN": TOKEN,
            "ADMIN_TG_ID": "42",
        }
        env.update(overrides)
        for key in [k for k, v in env.items() if v == "__unset__"]:
            del env[key]
        return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def curl_calls(self) -> int:
        return len((self.stub / "calls").read_text().splitlines())

    def backups(self) -> list[str]:
        return sorted(p.name for p in self.dest.iterdir())


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_success_creates_valid_backup_with_mode_600(env: Env) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    name = f"leads-{datetime.now().strftime('%Y%m%d')}.db"
    assert env.backups() == [name]
    path = env.dest / name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert con.execute("SELECT count(*) FROM leads").fetchone() == (2,)
    con.close()
    assert env.curl_calls() == 0


def test_second_run_same_day_uses_timestamped_name(env: Env) -> None:
    assert env.run().returncode == 0
    assert env.run().returncode == 0
    names = env.backups()
    assert len(names) == 2
    day = datetime.now().strftime("%Y%m%d")
    assert f"leads-{day}.db" in names
    other = next(n for n in names if n != f"leads-{day}.db")
    assert other.startswith(f"leads-{day}-") and other.endswith(".db")
    assert len(other) == len(f"leads-{day}-000000.db")


def test_rotation_keeps_newest_and_ignores_other_files(env: Env) -> None:
    env.dest.mkdir(parents=True)
    old = [f"leads-202601{d:02d}.db" for d in range(1, 11)]
    for name in old:
        (env.dest / name).write_text("x")
    for other in ("notes.txt", "leads-before.db", "leads_before_x.db", "leads-20260101.db.bak"):
        (env.dest / other).write_text("keep")
    result = env.run(KEEP="3")
    assert result.returncode == 0, result.stderr
    today = f"leads-{datetime.now().strftime('%Y%m%d')}.db"
    backups = [n for n in env.backups() if n.startswith("leads-2") and n.endswith(".db")]
    assert backups == sorted([*old[-2:], today])
    for other in ("notes.txt", "leads-before.db", "leads_before_x.db", "leads-20260101.db.bak"):
        assert (env.dest / other).read_text() == "keep"


def test_corrupt_db_fails_keeps_old_backups_and_alerts(env: Env) -> None:
    env.dest.mkdir(parents=True)
    for d in range(1, 6):
        (env.dest / f"leads-202601{d:02d}.db").write_text("x")
    env.db.write_bytes(b"this is not a sqlite database" * 100)
    result = env.run(KEEP="2")
    assert result.returncode != 0
    assert len([n for n in env.backups() if n.endswith(".db")]) == 5
    assert not [n for n in env.backups() if n.endswith(".tmp")]
    assert env.curl_calls() == 1
    config = (env.stub / "stdin_1").read_text()
    assert "Бэкап leads.db не удался" in config
    assert TOKEN in config
    assert "chat_id=42" in config
    assert TOKEN not in result.stdout + result.stderr


def test_missing_db_fails_and_alerts_without_leaking_token(env: Env) -> None:
    result = env.run(DB=str(env.root / "nonexistent.db"))
    assert result.returncode != 0
    assert not (env.root / "nonexistent.db").exists()
    assert env.curl_calls() == 1
    assert TOKEN not in result.stdout + result.stderr


def test_missing_leads_table_fails(env: Env) -> None:
    con = sqlite3.connect(env.db)
    con.execute("DROP TABLE leads")
    con.commit()
    con.close()
    result = env.run()
    assert result.returncode != 0
    assert not [n for n in env.backups() if n.endswith(".db")]
    assert env.curl_calls() == 1


def test_missing_token_fails_without_calling_curl(env: Env) -> None:
    result = env.run(DB=str(env.root / "nonexistent.db"), BOT_TOKEN="__unset__")
    assert result.returncode != 0
    assert env.curl_calls() == 0
    assert "BOT_TOKEN" in result.stderr


def test_failed_alert_keeps_nonzero_exit_without_leaking(env: Env) -> None:
    result = env.run(DB=str(env.root / "nonexistent.db"), CURL_EXIT="22")
    assert result.returncode != 0
    assert env.curl_calls() == 1
    assert "api.telegram.org" not in result.stdout + result.stderr
    assert TOKEN not in result.stdout + result.stderr
