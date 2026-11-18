"""
Share of selected organizations among the found ones, per niche, from the log of the bot.

    ssh root@SERVER 'journalctl -u spb-lead-bot --since "14 days ago" --no-pager' | python scripts/niche_stats.py
    python scripts/niche_stats.py bot.log [more.log]

The log has one line for every organization that was found ("Place [niche] outcome (id): reason", see
pipeline._log_place). An organization seen again counts once, with its best outcome: selected (new, known, chain)
beats not-a-lead. "Selected" means no working site: such an organization is a lead.
"""

import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

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


def main(argv: list[str]) -> int:
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
