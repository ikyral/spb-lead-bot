"""
Share of selected organizations among the found ones, per niche, from the log of the bot.

    ssh root@SERVER 'journalctl -u spb-lead-bot --since "14 days ago" --no-pager' | python scripts/niche_stats.py
    python scripts/niche_stats.py bot.log [more.log]

The log has one line for every organization that was found ("Place [niche] outcome (id): reason", see
pipeline._log_place). An organization seen again counts once, with its best outcome: selected (new, known, chain)
beats not-a-lead. "Selected" means no working site: such an organization is a lead.

Database mode, from a copy of leads.db (opened read-only):

    python scripts/niche_stats.py --db path/to/leads.db

First the shares of useful organizations from place_checks: every organization whose site 2gis_cdp checked, also
those with a working site, per niche and per district / search text. "Useful" is USEFUL_STATUSES of
src/services/site_status.py. The table exists only for data collected after it was added; a database without it
(or with it empty) gets a note, and the share of older data comes only from the log (the mode above).
Then saved leads per niche and per district / search text. Leads saved before query_text was kept, and leads of
sources without a search text (OSM), are counted under "unknown query". Chains are kept aside to count branches
and are not counted as leads.
"""

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

LINE_RE = re.compile(r"Place \[(?P<niche>[^\]]+)\] (?P<outcome>[a-z-]+) \((?P<id>[^\s()]+)\): (?P<reason>.*)$")
SELECTED = frozenset({"new", "known", "chain"})
# The best outcome wins when the same organization is met more than once.
RANK = {"new": 3, "known": 2, "chain": 1, "not-a-lead": 0}


@dataclass
class NicheStats:
    outcomes: dict[str, str] = field(default_factory=dict)  # organization id -> its best outcome
    reasons: Counter[str] = field(default_factory=Counter)  # why organizations were not selected

    @property
    def found(self) -> int:
        return len(self.outcomes)

    @property
    def selected(self) -> int:
        return sum(1 for outcome in self.outcomes.values() if outcome in SELECTED)

    @property
    def share(self) -> float:
        return self.selected / self.found if self.found else 0.0


def collect(lines: Iterable[str]) -> dict[str, NicheStats]:
    """Read the lines of a log; lines of other kinds are skipped."""
    stats: dict[str, NicheStats] = defaultdict(NicheStats)
    for line in lines:
        match = LINE_RE.search(line)
        if match is None:
            continue
        niche, outcome, org_id = match["niche"], match["outcome"], match["id"]
        if outcome not in RANK:
            continue
        niche_stats = stats[niche]
        if RANK[outcome] >= RANK.get(niche_stats.outcomes.get(org_id, ""), -1):
            niche_stats.outcomes[org_id] = outcome
        if outcome == "not-a-lead":
            reason = match["reason"]
            niche_stats.reasons["the site works" if reason.startswith("the site works") else "low score"] += 1
    return dict(stats)


def format_table(stats: dict[str, NicheStats]) -> str:
    rows = sorted(stats.items(), key=lambda item: (-item[1].share, -item[1].found))
    lines = [f"{'niche':20} {'found':>6} {'selected':>9} {'share':>7}  not selected because"]
    for niche, niche_stats in rows:
        why = ", ".join(f"{reason}: {count}" for reason, count in niche_stats.reasons.most_common()) or "-"
        lines.append(f"{niche:20} {niche_stats.found:>6} {niche_stats.selected:>9} {niche_stats.share:>6.0%}  {why}")
    return "\n".join(lines)


# ---------- the database mode ----------

UNKNOWN_QUERY = "unknown query"
NO_DISTRICT = "-"
DB_HEADER = (
    "Saved leads from the database (chains left out). Organizations with a working site are not saved,\n"
    "so the share of useful leads is not known here: run the script on the log for it."
)


@dataclass
class SavedStats:
    no_site: int = 0
    dead_site: int = 0
    other: int = 0

    @property
    def saved(self) -> int:
        return self.no_site + self.dead_site + self.other

    def add(self, site_status: str | None) -> None:
        if site_status == "no_site":
            self.no_site += 1
        elif site_status == "dead_site":
            self.dead_site += 1
        else:
            self.other += 1


# (niche, district, query text); None of a district or a query is shown as unknown.
SavedKey = tuple[str, str | None, str | None]


def count_saved(rows: Iterable[tuple[str, str | None, str | None, str | None]]) -> dict[SavedKey, SavedStats]:
    """Rows of (category, district, query_text, site_status) counted per niche, district and search text."""
    stats: dict[SavedKey, SavedStats] = defaultdict(SavedStats)
    for category, district, query_text, site_status in rows:
        query = query_text.strip() if query_text and query_text.strip() else None
        stats[(category, district or None, query)].add(site_status)
    return dict(stats)


def _stats_row(label: str, stats: SavedStats) -> str:
    return f"{label:60} {stats.saved:>6} {stats.no_site:>8} {stats.dead_site:>10} {stats.other:>6}"


def format_saved_table(stats: dict[SavedKey, SavedStats]) -> str:
    """Per niche: its total, then one line per district and search text, the largest first."""
    by_niche: dict[str, list[tuple[SavedKey, SavedStats]]] = defaultdict(list)
    for key, key_stats in stats.items():
        by_niche[key[0]].append((key, key_stats))
    totals: dict[str, SavedStats] = {}
    for niche, items in by_niche.items():
        total = SavedStats()
        for _, key_stats in items:
            total.no_site += key_stats.no_site
            total.dead_site += key_stats.dead_site
            total.other += key_stats.other
        totals[niche] = total

    head = f"{'niche / district / query':60} {'saved':>6} {'no_site':>8} {'dead_site':>10} {'other':>6}"
    lines = [DB_HEADER, "", head]
    for niche in sorted(totals, key=lambda name: (-totals[name].saved, name)):
        lines.append(_stats_row(niche, totals[niche]))
        items = sorted(by_niche[niche], key=lambda item: (-item[1].saved, item[0][1] or "", item[0][2] or ""))
        for (_, district, query), key_stats in items:
            label = f"  {district or NO_DISTRICT} / {query or UNKNOWN_QUERY}"
            lines.append(_stats_row(label, key_stats))
    return "\n".join(lines)


# ---------- shares from place_checks ----------

NO_CHECKS_NOTE = (
    "No checked organizations in place_checks (an older database, or nothing collected since the table was added):\n"
    "shares of useful organizations need data collected after this change. Below are saved leads only; for shares\n"
    "of older data run the script on the log."
)


def useful_statuses() -> frozenset[str]:
    """The statuses the project treats as a useful lead, from src/services/site_status.py."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)  # run as `python scripts/niche_stats.py`: the project root is not on the path
    from src.services.site_status import USEFUL_STATUSES

    return USEFUL_STATUSES


@dataclass
class CheckStats:
    checked: int = 0
    useful: int = 0

    @property
    def share(self) -> float:
        return self.useful / self.checked if self.checked else 0.0


def count_checks(
    rows: Iterable[tuple[str, str | None, str | None, str | None]], useful: frozenset[str]
) -> dict[SavedKey, CheckStats]:
    """Rows of (category, district, query_text, site_status) of place_checks, per niche, district and search text."""
    stats: dict[SavedKey, CheckStats] = defaultdict(CheckStats)
    for category, district, query_text, site_status in rows:
        query = query_text.strip() if query_text and query_text.strip() else None
        key_stats = stats[(category, district or None, query)]
        key_stats.checked += 1
        if site_status in useful:
            key_stats.useful += 1
    return dict(stats)


def _check_row(label: str, stats: CheckStats) -> str:
    return f"{label:60} {stats.checked:>8} {stats.useful:>7} {stats.share:>6.0%}"


def format_checks_table(stats: dict[SavedKey, CheckStats], useful: frozenset[str]) -> str:
    """Per niche: its share, then one line per district and search text, the best share first."""
    by_niche: dict[str, list[tuple[SavedKey, CheckStats]]] = defaultdict(list)
    for key, key_stats in stats.items():
        by_niche[key[0]].append((key, key_stats))
    totals = {
        niche: CheckStats(checked=sum(s.checked for _, s in items), useful=sum(s.useful for _, s in items))
        for niche, items in by_niche.items()
    }
    lines = [
        "Shares of useful organizations from place_checks (every organization whose site 2gis_cdp checked;",
        f"useful = {', '.join(sorted(useful))}).",
        "",
        f"{'niche / district / query':60} {'checked':>8} {'useful':>7} {'share':>6}",
    ]
    for niche in sorted(totals, key=lambda name: (-totals[name].share, -totals[name].checked, name)):
        lines.append(_check_row(niche, totals[niche]))
        items = sorted(by_niche[niche], key=lambda item: (-item[1].share, -item[1].checked, item[0][1] or ""))
        for (_, district, query), key_stats in items:
            lines.append(_check_row(f"  {district or NO_DISTRICT} / {query or UNKNOWN_QUERY}", key_stats))
    return "\n".join(lines)


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def read_check_rows(db_path: str) -> list[tuple[str, str | None, str | None, str | None]] | None:
    """(category, district, query_text, site_status) of place_checks; None if the table is not there yet."""
    connection = _connect_read_only(db_path)
    try:
        if not list(connection.execute("PRAGMA table_info(place_checks)")):
            return None
        cursor = connection.execute("SELECT category, district, query_text, site_status FROM place_checks")
        return [(str(row[0]), row[1], row[2], row[3]) for row in cursor]
    finally:
        connection.close()


# By (has query_text, has is_chain): older databases may lack either column.
_SAVED_QUERIES: dict[tuple[bool, bool], str] = {
    (True, True): (
        "SELECT category, district, query_text, site_status FROM leads WHERE is_chain IS NULL OR is_chain = 0"
    ),
    (True, False): "SELECT category, district, query_text, site_status FROM leads",
    (False, True): "SELECT category, district, NULL, site_status FROM leads WHERE is_chain IS NULL OR is_chain = 0",
    (False, False): "SELECT category, district, NULL, site_status FROM leads",
}


def read_saved_rows(db_path: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """(category, district, query_text, site_status) of saved leads; the file is opened read-only."""
    connection = _connect_read_only(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(leads)")}
        if not columns:
            raise sqlite3.OperationalError("no table leads")
        # A database the bot has not migrated yet has no query_text: all its leads are of an unknown query.
        cursor = connection.execute(_SAVED_QUERIES["query_text" in columns, "is_chain" in columns])
        return [(str(row[0]), row[1], row[2], row[3]) for row in cursor]
    finally:
        connection.close()


def main_db(db_path: str) -> int:
    try:
        checks = read_check_rows(db_path)
        rows = read_saved_rows(db_path)
    except sqlite3.Error as exc:
        print(f"Cannot read the leads of {db_path}: {exc}")
        return 1
    if checks:
        useful = useful_statuses()
        print(format_checks_table(count_checks(checks, useful), useful))
    else:
        print(NO_CHECKS_NOTE)
    print()
    if not rows:
        print("No saved leads in the database")
        return 0 if checks else 1
    print(format_saved_table(count_saved(rows)))
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--db":
        if len(argv) != 2:
            print("Usage: python scripts/niche_stats.py --db path/to/leads.db")
            return 2
        return main_db(argv[1])
    if argv:
        lines: list[str] = []
        for path in argv:
            # A log cut in the middle of a letter must not stop the count: such a line is only not read.
            with open(path, encoding="utf-8", errors="replace") as file:
                lines.extend(file)
    else:
        sys.stdin.reconfigure(errors="replace")  # type: ignore[union-attr]
        lines = list(sys.stdin)
    stats = collect(lines)
    if not stats:
        print("No lines of the form: Place [niche] outcome (id): reason")
        return 1
    print(format_table(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
