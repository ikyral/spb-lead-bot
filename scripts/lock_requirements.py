"""
Regenerate requirements.lock: the fully pinned runtime dependency set for the Docker image.

Resolves requirements.txt for the image platform (python:3.11-slim: CPython 3.11, Linux x86_64,
glibc 2.36) with pip's resolver, without installing anything, and writes "name==version" lines.

Usage (from the project root, any OS):

    python scripts/lock_requirements.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSION = "3.11"
ABI = "cp311"
# manylinux tags supported by Debian bookworm (glibc 2.36), newest first.
GLIBC_MINORS = range(36, 16, -1)

HEADER = f"""# Fully pinned runtime dependencies for the Docker image (python:{PYTHON_VERSION}-slim, Linux x86_64),
# resolved from requirements.txt. Do not edit by hand; regenerate after changing requirements.txt:
#
#   python scripts/lock_requirements.py
"""


def main() -> None:
    platforms: list[str] = []
    for minor in GLIBC_MINORS:
        platforms += ["--platform", f"manylinux_2_{minor}_x86_64"]
    platforms += ["--platform", "manylinux2014_x86_64"]

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                "--report", str(report),
                "--python-version", PYTHON_VERSION, "--implementation", "cp",
                "--abi", ABI, "--abi", "abi3", "--abi", "none",
                *platforms,
                "--only-binary=:all:",
                "-r", str(PROJECT_ROOT / "requirements.txt"),
            ],
            check=True,
        )
        installs = json.loads(report.read_text(encoding="utf-8"))["install"]

    pins = sorted(
        (item["metadata"]["name"], item["metadata"]["version"]) for item in installs
    )
    body = "".join(f"{name}=={version}\n" for name, version in sorted(pins, key=lambda p: p[0].lower()))
    (PROJECT_ROOT / "requirements.lock").write_text(HEADER + body, encoding="utf-8", newline="\n")
    print(f"requirements.lock: {len(pins)} packages")


if __name__ == "__main__":
    main()
