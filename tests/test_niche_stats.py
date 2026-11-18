"""The count of selected organizations per niche from the log (scripts/niche_stats.py)."""

import logging
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import niche_stats
from src.services import pipeline
from src.services.providers.base import RawPlace

LOG = """\
2026-11-19 05:40:01 [I] Place [auto_repair] not-a-lead (2gis_1): the site works (http://m.ru), status has_site
2026-11-19 05:40:01 [I] Place [auto_repair] new (2gis_2): site status no_site, score 55
2026-11-19 05:40:01 [I] Place [auto_repair] not-a-lead (2gis_3): score 5 is below 30 (site status dead_site)
2026-11-19 05:40:01 [I] Place [nails] known (2gis_4): the lead is already in the database
2026-11-19 05:40:01 [I] Place [nails] chain (2gis_5): a chain, saved aside to count its branches
2026-11-19 05:40:01 [I] Place [nails] not-a-lead (2gis_6): the site works (http://s.ru), status has_site
2026-11-19 05:40:02 [INFO] src.services.scheduler: Collection finished: status=ok found=6
2026-11-20 05:40:01 [I] Place [auto_repair] not-a-lead (2gis_1): the site works (http://m.ru), status has_site
2026-11-20 05:40:01 [I] Place [auto_repair] known (2gis_2): the lead is already in the database
"""


def test_the_share_counts_each_organization_once_with_its_best_outcome() -> None:
    stats = niche_stats.collect(LOG.splitlines())
    auto = stats["auto_repair"]
    # 3 organizations: Мотор (seen twice, never a lead), Гараж (new, then known), Д'Артаньян (low score)
    assert (auto.found, auto.selected) == (3, 1)
    assert auto.share == pytest.approx(1 / 3)
    assert auto.reasons == {"the site works": 2, "low score": 1}  # Мотор is counted in both of its sightings
    nails = stats["nails"]
    assert (nails.found, nails.selected) == (3, 2)  # known and a chain are selected too


def test_an_organization_that_became_a_lead_later_counts_as_selected() -> None:
    lines = [
        "Place [nails] not-a-lead (2gis_9): the site works (http://x.ru), status has_site",
        "Place [nails] new (2gis_9): site status no_site, score 40",
    ]
    stats = niche_stats.collect(lines)["nails"]
    assert (stats.found, stats.selected) == (1, 1)


def test_other_lines_are_skipped_and_the_table_is_sorted_by_share() -> None:
    table = niche_stats.format_table(niche_stats.collect(LOG.splitlines()))
    rows = table.splitlines()
    assert rows[0].startswith("niche")
    assert rows[1].startswith("nails") and "67%" in rows[1]  # the best share first
    assert rows[2].startswith("auto_repair") and "33%" in rows[2]
    assert niche_stats.collect(["Collection finished: found=6", "garbage"]) == {}


def test_main_reads_files_and_says_when_there_is_nothing(tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "bot.log"
    log.write_text(LOG, encoding="utf-8")
    assert niche_stats.main([str(log)]) == 0
    assert "auto_repair" in capsys.readouterr().out
    empty = tmp_path / "empty.log"
    empty.write_text("nothing here\n", encoding="utf-8")
    assert niche_stats.main([str(empty)]) == 1


@pytest.fixture
async def session(session_maker: Any) -> Any:
    async with session_maker() as open_session:
        yield open_session


async def test_the_pipeline_writes_lines_the_script_can_read(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def verdict(url: str | None, client: Any = None) -> tuple[str, None]:
        return ("site_found" if url else "no_site"), None

    monkeypatch.setattr(pipeline, "analyze_website", verdict)

    def place(number: int, website: str | None) -> RawPlace:
        return RawPlace(
            external_id=f"2gis_{number}",
            source="2gis",
            name=f"Организация {number}",
            category="tire_service",
            phone=f"+7 812 555-01-{number:02d}",
            raw_website=website,
            high_margin=True,
            contacts_known=True,
        )

    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        await pipeline._process_and_save_places(session, [place(1, "https://a.example"), place(2, None)])
    stats = niche_stats.collect(caplog.text.splitlines())["tire_service"]
    assert (stats.found, stats.selected) == (2, 1)
    assert stats.reasons == {"the site works": 1}


def test_a_log_cut_in_the_middle_of_a_letter_is_still_read(tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
    good = b"Place [nails] new (2gis_1): site status no_site, score 40\n"
    cut = "Place [nails] not-a-lead (2gis_2): the site works (http://сайт".encode()[:-1]  # half of a letter
    log = tmp_path / "cut.log"
    log.write_bytes(good + cut + b"\n")
    assert niche_stats.main([str(log)]) == 0
    assert "nails" in capsys.readouterr().out
