"""
Which niches are worth checking first, judged by the leads already checked.

All niches keep being collected. Here only the order of reading cards is decided: niches where a
site is often missing go first, and a part of every file goes to niches that have hardly been tried.
"""

import math
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Lead
from src.database.repo import get_known_niches, get_niche_site_counts, get_unknown_site_candidates
from src.services.providers.base import TARGET_CATEGORIES, niche_title
from src.services.site_status import NO_SITE_STATUSES, SITE_DEAD, SITE_FOUND, SITE_UNKNOWN

# A niche with fewer checked leads than this has not told anything yet.
EXPLORE_CHECKED_BELOW: int = 5
# The share of a file that goes to such niches, while there are any.
EXPLORE_SHARE: float = 0.2
MAX_LEADS_PER_NICHE: int = 10


def useful_share(checked: int, no_site: int, dead: int) -> float:
    """The smoothed share of leads without a working site: a niche nobody has checked gets 0.5."""
    return (no_site + dead + 1) / (checked + 2)


@dataclass(frozen=True)
class NicheStats:
    """What is known about the sites in one niche."""

    category: str
    no_site: int = 0
    dead: int = 0
    has_site: int = 0
    unknown: int = 0

    @property
    def checked(self) -> int:
        return self.no_site + self.dead + self.has_site

    @property
    def share(self) -> float:
        return useful_share(self.checked, self.no_site, self.dead)


@dataclass
class ExportPlan:
    """The leads of a file, in the order of reading. `explored` of them come first: a run may read only a part."""

    leads: list[Lead] = field(default_factory=list)
    explored: int = 0


def _sort_key(stats: NicheStats) -> tuple[float, str]:
    return (-stats.share, stats.category)


def stats_from_counts(counts: dict[str, dict[str, int]]) -> list[NicheStats]:
    """Statistics of niches from the counts of statuses, the most useful niches first."""
    stats = []
    for category, by_status in counts.items():
        stats.append(
            NicheStats(
                category=category,
                no_site=sum(by_status.get(status, 0) for status in NO_SITE_STATUSES),
                dead=by_status.get(SITE_DEAD, 0),
                has_site=by_status.get(SITE_FOUND, 0),
                unknown=by_status.get(SITE_UNKNOWN, 0),
            )
        )
    return sorted(stats, key=_sort_key)


async def load_niche_stats(session: AsyncSession) -> list[NicheStats]:
    return stats_from_counts(await get_niche_site_counts(session))


def plan_export(candidates: list[Lead], stats: list[NicheStats], limit: int) -> ExportPlan:
    """
    Choose up to `limit` leads: no more than MAX_LEADS_PER_NICHE of a niche, the most useful niches first.

    At least EXPLORE_SHARE of the file comes from niches with few checked leads, evenly from each,
    if there are that many. `candidates` keep their order within a niche.
    """
    known = {item.category: item for item in stats}
    by_niche: dict[str, list[Lead]] = {}
    for lead in candidates:
        by_niche.setdefault(lead.category, []).append(lead)
    order = sorted(
        (known.get(category, NicheStats(category=category)) for category in by_niche),
        key=_sort_key,
    )

    remaining = {category: leads[:MAX_LEADS_PER_NICHE] for category, leads in by_niche.items()}
    to_explore = [item.category for item in order if item.checked < EXPLORE_CHECKED_BELOW]
    quota = min(limit, math.ceil(limit * EXPLORE_SHARE))
    explored: list[Lead] = []
    while len(explored) < quota and any(remaining[category] for category in to_explore):
        for category in to_explore:
            if remaining[category] and len(explored) < quota:
                explored.append(remaining[category].pop(0))

    rest: list[Lead] = []
    for item in order:
        rest += remaining[item.category][: limit - len(explored) - len(rest)]
    return ExportPlan(leads=explored + rest, explored=len(explored))


async def choose_export(session: AsyncSession, limit: int, niche: str | None = None) -> ExportPlan:
    """The leads for a request file: of one niche if it is named, otherwise by the priority of niches."""
    candidates = await get_unknown_site_candidates(session, niche)
    if niche is not None:
        return ExportPlan(leads=candidates[:limit])
    return plan_export(candidates, await load_niche_stats(session), limit)


def _title_without_emoji(title: str) -> str:
    return "".join(char for char in title if char.isalnum() or char in " ,-").strip()


async def known_niches(session: AsyncSession) -> list[str]:
    """Every niche of the settings, and those that only the database has (free-text queries, CSV)."""
    return list(dict.fromkeys([*TARGET_CATEGORIES, *sorted(await get_known_niches(session))]))


def resolve_niche(text: str, niches: list[str]) -> str | None:
    """Find a niche by its key or its title, whatever the case. None if there is no such niche."""
    wanted = text.strip().casefold()
    for niche in niches:
        title = niche_title(niche)
        if wanted in (niche.casefold(), title.casefold(), _title_without_emoji(title).casefold()):
            return niche
    return None
