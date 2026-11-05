"""Entrypoint of the collector for a separate computer: see src/collector_cli.py and README."""

from src.collector_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
