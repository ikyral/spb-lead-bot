"""Tests for the container entrypoint that drops root privileges before starting the bot."""

import os

import pytest

import docker_entrypoint as entrypoint

APP_UID, APP_GID = 1000, 1000


@pytest.fixture
def fake_os(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Replace privilege-related os calls (absent on Windows) with recorders."""
    calls: list[tuple] = []
    db_dir = tmp_path / "db"

    monkeypatch.setattr(entrypoint, "WRITABLE_DIRS", (str(db_dir),))
    monkeypatch.setattr(entrypoint, "_lookup_user", lambda name: (APP_UID, APP_GID, "/app"))
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append(("chown", path, uid, gid)), raising=False)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", groups)), raising=False)
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("setgid", gid)), raising=False)
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("setuid", uid)), raising=False)
    monkeypatch.setattr(os, "execvp", lambda file, args: calls.append(("execvp", file, args)))
    return calls, db_dir


def test_root_fixes_db_ownership_drops_privileges_then_execs(monkeypatch: pytest.MonkeyPatch, fake_os) -> None:
    calls, db_dir = fake_os
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    db_dir.mkdir()
    (db_dir / "leads.db").write_bytes(b"")  # e.g. created earlier by a root container

    entrypoint.main(["python", "main.py"])

    chowned = {call[1] for call in calls if call[0] == "chown"}
    assert chowned == {str(db_dir), os.path.join(str(db_dir), "leads.db")}
    assert all(call[2:] == (APP_UID, APP_GID) for call in calls if call[0] == "chown")

    # Order matters: groups and gid must be dropped while still root, uid last, exec after that.
    order = [call[0] for call in calls if call[0] != "chown"]
    assert order == ["setgroups", "setgid", "setuid", "execvp"]
    assert calls[-1] == ("execvp", "python", ["python", "main.py"])
    assert os.environ["HOME"] == "/app"


def test_root_creates_missing_db_dir(monkeypatch: pytest.MonkeyPatch, fake_os) -> None:
    calls, db_dir = fake_os
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)

    entrypoint.main(["python", "main.py"])

    assert db_dir.is_dir()
    assert ("chown", str(db_dir), APP_UID, APP_GID) in calls


def test_chown_failure_is_a_warning_not_a_crash(monkeypatch: pytest.MonkeyPatch, fake_os, capsys) -> None:
    """Some bind mounts (e.g. Docker Desktop on Windows) reject chown but are writable anyway."""
    calls, _db_dir = fake_os
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)

    def deny_chown(path, uid, gid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chown", deny_chown, raising=False)

    entrypoint.main(["python", "main.py"])

    assert "cannot change owner" in capsys.readouterr().err
    assert calls[-1][0] == "execvp"


def test_non_root_skips_privilege_changes(monkeypatch: pytest.MonkeyPatch, fake_os) -> None:
    """A container started with an explicit non-root 'user:' just runs the command."""
    calls, db_dir = fake_os
    monkeypatch.setattr(os, "getuid", lambda: APP_UID, raising=False)
    db_dir.mkdir()

    entrypoint.main(["python", "main.py"])

    assert calls == [("execvp", "python", ["python", "main.py"])]


def test_unwritable_db_dir_fails_with_actionable_message(monkeypatch: pytest.MonkeyPatch, fake_os) -> None:
    calls, db_dir = fake_os
    monkeypatch.setattr(os, "getuid", lambda: APP_UID, raising=False)
    db_dir.mkdir()
    (db_dir / "leads.db").write_bytes(b"")
    monkeypatch.setattr(os, "access", lambda path, mode: not path.endswith("leads.db"))

    with pytest.raises(SystemExit, match=r"leads\.db is not writable.*chown"):
        entrypoint.main(["python", "main.py"])
    assert not any(call[0] == "execvp" for call in calls)


def test_requires_command() -> None:
    with pytest.raises(SystemExit, match="usage"):
        entrypoint.main([])
