"""End-to-end skeleton smoke test — no network. Proves the seams wire together:
DB init + FTS5, durable queue dedup, rate limiter, query normalization, facet aggregation.
"""
from __future__ import annotations

import asyncio
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest

from packages.core.enums import JobStatus, JobType, RateBucket, SearchType
from packages.core.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_db():
    # in-memory DB is created per-engine; get_settings is cached so the URL sticks.
    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import packages.storage.db as db

    db._engine = None
    db._SessionFactory = None
    db.init_db()
    yield


def test_db_init_creates_fts():
    import packages.storage.db as db
    from sqlalchemy import text

    with db.get_engine().begin() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table','view')"))}
    assert "matches" in names
    assert "fts_matches" in names
    assert "asset_history" in names


def test_queue_dedup():
    from packages.indexer.queue import enqueue

    first = enqueue(JobType.DISCOVERY, "discovery:1", {"search_id": 1})
    again = enqueue(JobType.DISCOVERY, "discovery:1", {"search_id": 1})
    assert first == again  # deduped to the same job, id returned both times


def test_search_readiness_reconcile():
    import packages.storage.db as db
    from packages.indexer import queue as q
    from packages.storage.orm import Search
    from packages.storage.repositories import SqlSearchStore

    with db.session_scope() as s:
        search = SqlSearchStore(s).create(
            keyword="HighlightPlus", normalized_query="HighlightPlus", search_type="keyword"
        )
        search_id = search.id

    job_id = q.enqueue(JobType.REPO_ENRICHMENT, "enrich:42", {"repo_id": 42})
    q.link_search_job(search_id, job_id)
    q.link_search_job(search_id, job_id)  # idempotent

    with db.session_scope() as s:
        SqlSearchStore(s).set_status(search_id, "enriching")

    assert q.reconcile_search_readiness() == 0  # linked job still queued

    claimed = q.claim_next()
    assert claimed is not None and claimed["id"] == job_id
    assert q.reconcile_search_readiness() == 0  # running is still pending
    q.complete(job_id, JobStatus.DONE)

    assert q.reconcile_search_readiness() == 1
    with db.session_scope() as s:
        row = s.get(Search, search_id)
        assert row.status == "ready"
        assert row.completed_at is not None


def test_path_search_finds_folder_names():
    """Folder-name search must use `kw in:path` — bare `path:kw` does not match folder
    names on the legacy code-search API (verified empirically against GitHub)."""
    from packages.core.query import normalize

    assert normalize("Exoa", SearchType.PATH).normalized == "Exoa in:path"
    assert normalize("HighlightPlus", SearchType.KEYWORD).normalized == "HighlightPlus"


def test_index_reuses_inflight_or_ready_search():
    from fastapi.testclient import TestClient

    from apps.api.main import app

    with TestClient(app) as c:
        first = c.post("/index", json={"keyword": "ReUseMe"}).json()
        again = c.post("/index", json={"keyword": "ReUseMe"}).json()
        assert first["search_id"] == again["search_id"]  # no duplicate rows
        other = c.post("/index", json={"keyword": "ReUseMe", "search_type": "path"}).json()
        assert other["search_id"] != first["search_id"]  # different type = new search


def test_delete_search_removes_row_and_its_job():
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    import packages.storage.db as db
    from apps.api.main import app
    from packages.storage.orm import Job

    with TestClient(app) as c:
        sid = c.post("/index", json={"keyword": "DeleteMe"}).json()["search_id"]
        assert c.delete(f"/searches/{sid}").status_code == 204
        assert all(
            s["search_id"] != sid for s in c.get("/searches").json()["searches"]
        )
        assert c.delete(f"/searches/{sid}").status_code == 404
    with db.session_scope() as s:
        # The job ROW must be gone: SQLite reuses row ids, and a lingering
        # discovery:{id} key would block a future search's enqueue forever.
        assert s.scalar(select(Job).where(Job.dedup_key == f"discovery:{sid}")) is None


def test_index_reuse_is_case_insensitive():
    from fastapi.testclient import TestClient

    from apps.api.main import app

    with TestClient(app) as c:
        first = c.post("/index", json={"keyword": "Exoa", "search_type": "path"}).json()
        again = c.post("/index", json={"keyword": "exoa", "search_type": "path"}).json()
    assert first["search_id"] == again["search_id"]  # GitHub search is case-insensitive


def test_index_repairs_pending_search_with_stale_job():
    """A pending search whose discovery job row is stale (skipped/done/error — e.g. a
    reused-id leftover that may even carry ANOTHER search's query in its payload) must
    get a fresh, correctly-parameterised job on re-submit instead of hanging forever."""
    import json as _json

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    import packages.storage.db as db
    from apps.api.main import app
    from packages.storage.orm import Job

    with TestClient(app) as c:
        sid = c.post("/index", json={
            "keyword": "StuckPending", "search_type": "path",
        }).json()["search_id"]
        with db.session_scope() as s:
            job = s.scalar(select(Job).where(Job.dedup_key == f"discovery:{sid}"))
            # Simulate the live incident: the leftover job is DONE and carries the
            # deleted predecessor's (different!) query.
            job.status = "done"
            job.payload = _json.dumps({"search_id": sid, "normalized_query": "old-query"})
        resub = c.post("/index", json={
            "keyword": "StuckPending", "search_type": "path",
        }).json()
        assert resub["search_id"] == sid
    with db.session_scope() as s:
        job = s.scalar(select(Job).where(Job.dedup_key == f"discovery:{sid}"))
        assert job is not None and job.status == "queued"
        payload = _json.loads(job.payload)
        assert payload["normalized_query"] == "StuckPending in:path"  # THIS search's query


async def test_discovery_streams_files_per_page():
    """on_files must fire once per absorbed page so the caller can persist results
    incrementally — the fix for 'nothing appears until the whole collection ends'."""
    from packages.indexer.discovery import DiscoveryEngine

    class FakeGH:
        async def search_code(self, query, *, page, per_page):
            return {
                "total_count": 250,  # 3 pages at 100/page
                "items": [{
                    "repository": {"id": 1, "full_name": "a/b", "language": "C#"},
                    "path": f"p{page}-{i}.cs", "sha": "s",
                } for i in range(5)],
            }

    batches: list[int] = []
    files, stats = await DiscoveryEngine(
        FakeGH(), on_files=lambda b: batches.append(len(b))
    ).run("x")
    assert batches == [5, 5, 5]  # one callback per page, as absorbed
    assert stats.collected == 15 == len(files)


async def test_discovery_cancels_via_progress_callback():
    """A long collection stops as soon as the callback says the search is gone."""
    from packages.indexer.discovery import DiscoveryEngine

    class FakeGH:
        async def search_code(self, query, *, page, per_page):
            return {
                "total_count": 900,
                "items": [{
                    "repository": {"id": 1, "full_name": "a/b", "language": "C#"},
                    "path": f"f{page}.cs", "sha": "s",
                }] * 3,
            }

    calls = {"n": 0}

    def progress(stats) -> bool:
        calls["n"] += 1
        return calls["n"] <= 2  # allow two GitHub calls, then cancel

    files, stats = await DiscoveryEngine(FakeGH(), progress_cb=progress).run("x")
    assert stats.cancelled is True
    assert stats.search_calls == 2  # third call never fired
    assert len(files) > 0  # partial results retained (caller decides to drop them)


def test_orphaned_running_jobs_requeued_on_startup():
    """A job claimed by a worker when the process dies must not hang forever — startup
    recovery requeues it (this exact bug left searches stuck at 'discovering')."""
    from packages.indexer import queue as q

    q.enqueue(JobType.DISCOVERY, "discovery:900", {"search_id": 900})
    claimed = q.claim_next()
    assert claimed is not None  # now 'running', simulating a crash before completion

    assert q.recover_orphaned_jobs() == 1
    reclaimed = q.claim_next()
    assert reclaimed is not None and reclaimed["dedup_key"] == "discovery:900"
    # Orphaning is a restart artifact, not a failure — attempts reset so repeated
    # restarts can't push a healthy job to its terminal-error cap.
    assert reclaimed["attempts"] == 1


def test_repair_flips_dead_discovery_search_to_error():
    import packages.storage.db as db
    from packages.indexer import queue as q
    from packages.storage.orm import Search
    from packages.storage.repositories import SqlSearchStore

    with db.session_scope() as s:
        search = SqlSearchStore(s).create(
            keyword="doomed", normalized_query="doomed", search_type="keyword"
        )
        SqlSearchStore(s).set_status(search.id, "discovering")
        search_id = search.id

    job_id = q.enqueue(JobType.DISCOVERY, f"discovery:{search_id}", {"search_id": search_id})
    q.claim_next()
    q.complete(job_id, JobStatus.ERROR)  # terminal death while app was 'down'

    assert q.repair_search_statuses() == 1
    with db.session_scope() as s:
        assert s.get(Search, search_id).status == "error"


def test_search_with_no_jobs_becomes_ready():
    """Zero-result discovery emits no jobs — the search must not hang in 'enriching'."""
    import packages.storage.db as db
    from packages.indexer import queue as q
    from packages.storage.orm import Search
    from packages.storage.repositories import SqlSearchStore

    with db.session_scope() as s:
        search = SqlSearchStore(s).create(
            keyword="nohits", normalized_query="nohits", search_type="keyword"
        )
        SqlSearchStore(s).set_status(search.id, "enriching")
        search_id = search.id

    assert q.reconcile_search_readiness() == 1
    with db.session_scope() as s:
        assert s.get(Search, search_id).status == "ready"


def test_query_normalization():
    from packages.core.query import normalize

    assert normalize("HighlightPlus", SearchType.KEYWORD).normalized == "HighlightPlus"
    assert normalize("using DOTween", SearchType.PHRASE).normalized == '"using DOTween"'
    assert normalize(".shader", SearchType.EXTENSION).normalized == "extension:shader"
    assert (
        normalize("Assets/HighlightPlus", SearchType.PATH).normalized
        == "Assets/HighlightPlus in:path"
    )


def test_search_bucket_does_not_burst():
    """GitHub's code-search limiter punishes clumping harder than volume: 8 calls in two
    seconds earns secondary-limit 403s (and degraded empty result sets) even though the
    same 8 calls spread over a minute are fine. The search bucket must therefore pace
    strictly, while cheap buckets keep their burst."""
    from packages.github.ratelimit import RateLimiter

    limiter = RateLimiter(search_rpm=30, core_rpm=60, graphql_rpm=60)

    async def _paced():
        await asyncio.wait_for(limiter.acquire(RateBucket.SEARCH), timeout=1.0)
        # The second search call must WAIT ~60/30s rather than fire immediately.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire(RateBucket.SEARCH), timeout=0.5)
        # Core is not the scarce resource — it still bursts.
        for _ in range(5):
            await asyncio.wait_for(limiter.acquire(RateBucket.CORE), timeout=1.0)

    asyncio.run(_paced())


def test_facets_on_seeded_results():
    import packages.storage.db as db
    from packages.storage.repositories import (
        SqlMatchStore,
        SqlRepoStore,
        SqlSearchStore,
        compute_facets,
    )

    with db.session_scope() as s:
        repo = SqlRepoStore(s).upsert_shallow(github_id=1, full_name="acme/game")
        SqlRepoStore(s).upsert_metadata(1, full_name="acme/game", primary_language="C#",
                                        license_spdx="MIT", is_fork=False)
        m = SqlMatchStore(s).upsert(repo_id=repo.id, path="Assets/HighlightPlus/HP.cs",
                                    detected_language="C#")
        search = SqlSearchStore(s).create(keyword="HighlightPlus",
                                          normalized_query="HighlightPlus", search_type="keyword")
        SqlSearchStore(s).attach_result(search.id, m.id, 1)
        search_id = search.id

    with db.session_scope() as s:
        facets = compute_facets(s, search_id)
    langs = {f["value"] for f in facets["languages"]}
    prefixes = {f["value"] for f in facets["path_prefixes"]}
    assert "C#" in langs
    assert "Assets/HighlightPlus" in prefixes


def test_server_side_facet_filters():
    """apply_facet_filters: OR within a group, AND across groups, unknown groups ignored."""
    import packages.storage.db as db
    from sqlalchemy import select
    from packages.storage.orm import Match, Repository, SearchResult
    from packages.storage.repositories import (
        SqlMatchStore,
        SqlRepoStore,
        SqlSearchStore,
        apply_facet_filters,
    )

    with db.session_scope() as s:
        r1 = SqlRepoStore(s).upsert_shallow(github_id=1, full_name="acme/cs")
        SqlRepoStore(s).upsert_metadata(1, full_name="acme/cs", license_spdx="MIT")
        r2 = SqlRepoStore(s).upsert_shallow(github_id=2, full_name="globex/go")
        SqlRepoStore(s).upsert_metadata(2, full_name="globex/go", license_spdx="Apache-2.0")
        m1 = SqlMatchStore(s).upsert(repo_id=r1.id, path="Assets/HP/A.cs",
                                     detected_language="C#", extension=".cs")
        m2 = SqlMatchStore(s).upsert(repo_id=r2.id, path="pkg/main.go",
                                     detected_language="Go", extension=".go")
        search = SqlSearchStore(s).create(keyword="k", normalized_query="k",
                                          search_type="keyword")
        SqlSearchStore(s).attach_result(search.id, m1.id, 1)
        SqlSearchStore(s).attach_result(search.id, m2.id, 2)
        search_id = search.id

    def run(filters) -> set[str]:
        with db.session_scope() as s:
            stmt = (
                select(Match.path)
                .select_from(SearchResult)
                .join(Match, Match.id == SearchResult.match_id)
                .join(Repository, Repository.id == Match.repo_id)
                .where(SearchResult.search_id == search_id)
            )
            stmt = apply_facet_filters(stmt, filters)
            return {row[0] for row in s.execute(stmt).all()}

    assert run({}) == {"Assets/HP/A.cs", "pkg/main.go"}
    assert run({"languages": ["C#"]}) == {"Assets/HP/A.cs"}
    assert run({"owners": ["globex"]}) == {"pkg/main.go"}
    assert run({"languages": ["C#", "Go"]}) == {"Assets/HP/A.cs", "pkg/main.go"}  # OR in-group
    assert run({"languages": ["C#", "Go"], "licenses": ["MIT"]}) == {"Assets/HP/A.cs"}  # AND
    assert run({"extensions": ["nope"]}) == set()
    assert run({"bogus_group": ["x"]}) == {"Assets/HP/A.cs", "pkg/main.go"}  # ignored


def test_facets_cross_filter():
    """Facet counts reflect OTHER groups' filters but not a group's own selection."""
    import packages.storage.db as db
    from packages.storage.repositories import (
        SqlMatchStore,
        SqlRepoStore,
        SqlSearchStore,
        compute_facets,
    )

    # Two C# repos (one MIT, one Apache) + one Go repo (MIT).
    seed = [
        (1, "a/cs-mit", "MIT", "C#", ".cs"),
        (2, "b/cs-apache", "Apache-2.0", "C#", ".cs"),
        (3, "c/go-mit", "MIT", "Go", ".go"),
    ]
    with db.session_scope() as s:
        search = SqlSearchStore(s).create(keyword="k", normalized_query="k",
                                          search_type="keyword")
        for i, (gid, full, lic, lang, ext) in enumerate(seed, start=1):
            r = SqlRepoStore(s).upsert_shallow(github_id=gid, full_name=full)
            SqlRepoStore(s).upsert_metadata(gid, full_name=full, license_spdx=lic)
            m = SqlMatchStore(s).upsert(repo_id=r.id, path=f"src/f{gid}{ext}",
                                        detected_language=lang, extension=ext)
            SqlSearchStore(s).attach_result(search.id, m.id, i)
        search_id = search.id

    def counts(facets, group):
        return {f["value"]: f["count"] for f in facets[group]}

    with db.session_scope() as s:
        # No filter: every group counts the whole set.
        base = compute_facets(s, search_id)
        assert counts(base, "languages") == {"C#": 2, "Go": 1}
        assert counts(base, "licenses") == {"MIT": 2, "Apache-2.0": 1}

        # Filter licenses=MIT: language counts drop the Apache C# repo (cross-filter),
        # but the licenses group itself still lists BOTH licenses (excludes its own).
        mit = compute_facets(s, search_id, filters={"licenses": ["MIT"]})
        assert counts(mit, "languages") == {"C#": 1, "Go": 1}
        assert counts(mit, "licenses") == {"MIT": 2, "Apache-2.0": 1}

        # Filter languages=C#: license counts drop the Go/MIT repo; languages group
        # still shows both languages so you can widen the C# selection.
        cs = compute_facets(s, search_id, filters={"languages": ["C#"]})
        assert counts(cs, "licenses") == {"MIT": 1, "Apache-2.0": 1}
        assert counts(cs, "languages") == {"C#": 2, "Go": 1}


def test_quit_never_signals_a_recycled_pid(tmp_path, monkeypatch):
    """The Quit button stops the dev server by PID, which means it must prove the PID
    still belongs to that dev server. A stale `.run/web.pid` whose number has since been
    reused by an unrelated process must be ignored, not signalled."""
    import os

    from apps.api.main import _stop_web_dev_server

    monkeypatch.chdir(tmp_path)
    assert _stop_web_dev_server() is None  # no pid file at all

    run_dir = tmp_path / ".run"
    run_dir.mkdir()
    # Our own PID: a live process, but a python one — not the web server.
    (run_dir / "web.pid").write_text(str(os.getpid()))
    assert _stop_web_dev_server() is None
    assert (run_dir / "web.pid").exists()  # left alone, not consumed

    (run_dir / "web.pid").write_text("not-a-pid")
    assert _stop_web_dev_server() is None
