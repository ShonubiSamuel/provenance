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

from packages.core.enums import JobType, SearchStatus
from packages.core.settings import get_settings
from packages.indexer.stages import run_discovery
from packages.storage.db import init_db, session_scope
from packages.storage.orm import Job, Search, SearchResult
from packages.storage.repositories import SqlRepoStore, SqlSearchStore


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

    def __init__(self, count: int, pushed_at: str | None = None) -> None:
        self.count = count
        self.pages_served = 0
        self.health = _Health()
        self.pushed_at = pushed_at

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100, **_) -> dict:
        self.pages_served += 1
        start = (page - 1) * per_page
        items = [
            {
                "repository": {
                    "id": i, "full_name": f"owner{i}/repo{i}", "language": "C#",
                    "pushed_at": self.pushed_at,
                },
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


def _enrichment_jobs() -> list[str]:
    with session_scope() as s:
        rows = s.query(Job).filter(Job.type == str(JobType.REPO_ENRICHMENT)).all()
        return [j.dedup_key for j in rows]


def _mark_enriched(github_id: int, pushed_at: str) -> None:
    """Simulate a completed enrichment: what upsert_metadata() would set."""
    with session_scope() as s:
        SqlRepoStore(s).upsert_metadata(github_id, pushed_at=_iso(pushed_at))


def _iso(s: str):
    from dateutil import parser as dtparse

    return dtparse.isoparse(s)


@pytest.mark.asyncio
async def test_rediscovering_an_unchanged_repo_does_not_requeue_enrichment():
    """A repo already enriched, re-found by a later search with the same pushed_at,
    must not spend another enrichment job — the whole point of needs_enrichment()."""
    search_id = _new_search("Thing", "Thing")
    gh = HealthyGH(1, pushed_at="2024-01-01T00:00:00Z")
    await run_discovery(gh, {"search_id": search_id, "normalized_query": "Thing"})

    jobs_after_first = _enrichment_jobs()
    assert len(jobs_after_first) == 1  # brand-new repo: always enriched once

    _mark_enriched(0, "2024-01-01T00:00:00Z")

    search_id_2 = _new_search("Thing", "Thing")
    gh2 = HealthyGH(1, pushed_at="2024-01-01T00:00:00Z")  # unchanged
    await run_discovery(gh2, {"search_id": search_id_2, "normalized_query": "Thing"})

    assert _enrichment_jobs() == jobs_after_first  # no new job — nothing changed


@pytest.mark.asyncio
async def test_rediscovering_a_repo_pushed_since_last_enrichment_requeues_it():
    """A repo that has genuinely gained commits since it was last enriched must get a
    fresh enrichment job — stale stars/forks/license must not persist forever."""
    search_id = _new_search("Thing", "Thing")
    gh = HealthyGH(1, pushed_at="2024-01-01T00:00:00Z")
    await run_discovery(gh, {"search_id": search_id, "normalized_query": "Thing"})
    _mark_enriched(0, "2024-01-01T00:00:00Z")

    search_id_2 = _new_search("Thing", "Thing")
    gh2 = HealthyGH(1, pushed_at="2024-06-01T00:00:00Z")  # pushed since
    await run_discovery(gh2, {"search_id": search_id_2, "normalized_query": "Thing"})

    jobs = _enrichment_jobs()
    assert len(jobs) == 2  # the original job plus a fresh one for the new push
