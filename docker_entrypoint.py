"""
Container entrypoint: prepare writable directories, drop root privileges, run the command.

The container starts as root only so it can fix ownership of the bind-mounted ./db
(Docker creates a missing host directory as root, and databases created by older
root-run images are root-owned). The bot itself always runs as the unprivileged
APP_USER. If the container is started with a non-root 'user:', privileges are
already dropped and the command is executed as is.
"""

import os
import sys

APP_USER = "app"
# Directories the bot writes to; /app/db is the volume from docker-compose.yml (DB_URL).
WRITABLE_DIRS: tuple[str, ...] = ("/app/db",)


def _lookup_user(name: str) -> tuple[int, int, str]:
    """Return (uid, gid, home) of a system user."""
    import pwd  # POSIX-only; imported lazily so the module can be tested on any OS

    entry = pwd.getpwnam(name)
    return entry.pw_uid, entry.pw_gid, entry.pw_dir


def _fix_ownership(path: str, uid: int, gid: int) -> None:
    """Create the directory if needed and chown it and its files to uid:gid."""
    os.makedirs(path, exist_ok=True)
    for root, _dirs, files in os.walk(path):
        for target in (root, *(os.path.join(root, name) for name in files)):
            try:
                stat = os.stat(target)
                if (stat.st_uid, stat.st_gid) != (uid, gid):
                    os.chown(target, uid, gid)
            except OSError as exc:
                print(f"entrypoint: warning: cannot change owner of {target}: {exc}", file=sys.stderr)


def _drop_privileges(uid: int, gid: int, home: str) -> None:
    """Switch to uid/gid irreversibly (supplementary groups and gid first, while still root)."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = home


def _check_writable(dirs: tuple[str, ...]) -> None:
    """Fail with an actionable message instead of SQLite's 'unable to open database file'."""
    for directory in dirs:
        targets = [directory]
        if os.path.isdir(directory):
            targets += [
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, name))
            ]
        for target in targets:
            if not os.access(target, os.W_OK):
                raise SystemExit(
                    f"entrypoint: {target} is not writable by uid {os.getuid()}. "
                    "On the host run: sudo chown -R <APP_UID>:<APP_GID> ./db "
                    "(APP_UID/APP_GID are build args, 1000 by default)."
                )


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: docker_entrypoint.py COMMAND [ARGS...]")

    if os.getuid() == 0:
        uid, gid, home = _lookup_user(APP_USER)
        for directory in WRITABLE_DIRS:
            _fix_ownership(directory, uid, gid)
        _drop_privileges(uid, gid, home)

    _check_writable(WRITABLE_DIRS)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
