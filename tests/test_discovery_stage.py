"""The discovery STAGE end to end: GitHub responses in, database rows out.

`discovery.py` is tested in isolation elsewhere; this covers the part that was actually
broken in the field — what the user ends up seeing. A collection must stream rows into
the index as pages land, and a collection that fails must land in `error` with a sentence
explaining why, never in a state that renders as a spinner or as "0 matches".
"""
from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest

from packages.core.enums import SearchStatus
from packages.core.settings import get_settings
from packages.indexer.stages import run_discovery
from packages.storage.db import init_db, session_scope
from packages.storage.orm import Search, SearchResult
from packages.storage.repositories import SqlSearchStore


@pytest.fixture(autouse=True)
def _fresh_db():
    get_settings.cache_clear()
    import packages.storage.db as db

    db._engine = None
    db._SessionFactory = None
    init_db()
    yield


def _new_search(keyword: str, normalized: str) -> int:
    with session_scope() as s:
        return SqlSearchStore(s).create(
            keyword=keyword, normalized_query=normalized, search_type="keyword"
        ).id


def _search_row(search_id: int) -> dict:
    with session_scope() as s:
        row = s.get(Search, search_id)
        results = s.query(SearchResult).filter(SearchResult.search_id == search_id).count()
        return {
            "status": row.status,
            "note": row.note,
            "collected": row.total_matches,
            "reported": row.reported_matches,
            "sampled": row.sampled,
            "rows": results,
        }


class HealthyGH:
    """Serves a page at a time, like GitHub on a good day."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.pages_served = 0
        self.health = _Health()

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100, **_) -> dict:
        self.pages_served += 1
        start = (page - 1) * per_page
        items = [
            {
                "repository": {"id": i, "full_name": f"owner{i}/repo{i}", "language": "C#"},
                "path": f"Assets/Thing/File{i}.cs",
                "sha": f"sha{i}",
            }
            for i in range(start, min(start + per_page, self.count))
        ]
        return {"total_count": self.count, "items": items}

    async def code_search_alive(self) -> bool:
        return True


class _Health:
    degraded_since = None


class DegradedGH(HealthyGH):
    """Reports matches, returns none — the failure that produced an endless spinner."""

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100, **_) -> dict:
        self.pages_served += 1
        return {"total_count": 1304, "items": []}


class SilentGH(HealthyGH):
    """Reports zero for everything, including the canary — indistinguishable from an
    honest empty result until you ask the control question."""

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100, **_) -> dict:
        self.pages_served += 1
        return {"total_count": 0, "items": []}

    async def code_search_alive(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_results_are_persisted_as_pages_land():
    search_id = _new_search("Thing", "Thing")
    gh = HealthyGH(250)

    await run_discovery(gh, {"search_id": search_id, "normalized_query": "Thing"})

    row = _search_row(search_id)
    assert row["rows"] == 250            # every match indexed…
    assert row["collected"] == 250       # …and counted honestly
    assert row["reported"] == 250
    assert row["note"] is None           # nothing to apologise for
    assert row["status"] in (str(SearchStatus.ENRICHING), str(SearchStatus.READY))


@pytest.mark.asyncio
async def test_degraded_github_ends_in_error_with_an_explanation():
    search_id = _new_search("MeshCombiner", "MeshCombiner")
    gh = DegradedGH(0)

    await run_discovery(gh, {"search_id": search_id, "normalized_query": "MeshCombiner"})

    row = _search_row(search_id)
    assert row["status"] == str(SearchStatus.ERROR)  # not "ready with 0 results"
    assert row["reported"] == 1304                   # what GitHub claimed…
    assert row["collected"] == 0                     # …and what we got
    assert "returned none" in row["note"]
    assert gh.pages_served <= 4  # aborts quickly instead of grinding the quota away


@pytest.mark.asyncio
async def test_zero_results_are_verified_against_a_canary_before_being_believed():
    search_id = _new_search("Nothing", "Nothing")
    gh = SilentGH(0)

    await run_discovery(gh, {"search_id": search_id, "normalized_query": "Nothing"})

    row = _search_row(search_id)
    assert row["status"] == str(SearchStatus.ERROR)
    assert "control query" in row["note"]


@pytest.mark.asyncio
async def test_a_genuinely_empty_search_is_reported_as_empty():
    """The canary must not turn honest 'no matches' into a scary error."""
    search_id = _new_search("Nothing", "Nothing")
    gh = SilentGH(0)
    gh.code_search_alive = _true  # index is fine; the term simply has no matches

    await run_discovery(gh, {"search_id": search_id, "normalized_query": "Nothing"})

    row = _search_row(search_id)
    assert row["status"] in (str(SearchStatus.ENRICHING), str(SearchStatus.READY))
    assert row["note"] is None
    assert row["collected"] == 0


async def _true() -> bool:
    return True


@pytest.mark.asyncio
async def test_absurdly_broad_query_is_sampled_and_says_so():
    search_id = _new_search("unity", "unity")
    gh = HealthyGH(212_860_928)

    await run_discovery(gh, {"search_id": search_id, "normalized_query": "unity"})

    row = _search_row(search_id)
    assert row["sampled"] is True
    assert row["rows"] == 1000           # the ceiling, in hand
    assert row["reported"] == 212_860_928
    assert "too many to index" in row["note"]
    assert gh.pages_served == 10  # ten pages, not a 300-call bisection into nothing
