"""Download-manager tests — no aria2 process, no network.

Endpoints run through TestClient with the GitHub client and download manager overridden
by fakes; history/reconcile logic runs against the real in-memory SQLite. Covers URL
resolution, folder fan-out, persistent history aggregation, and id-based actions.
"""
from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from packages.downloads.manager import DownloadView, _safe_component, _safe_subpath


@pytest.fixture(autouse=True)
def _fresh_db():
    from packages.core.settings import get_settings

    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import packages.storage.db as db

    db._engine = None
    db._SessionFactory = None
    db.init_db()
    yield


# --------------------------------------------------------------------------- #
# Pure: path sanitizers (traversal guards for the download dir)
# --------------------------------------------------------------------------- #
def test_safe_component_strips_traversal():
    assert _safe_component("main.py") == "main.py"
    assert _safe_component("../../etc/passwd") == "passwd"
    assert _safe_component("..") == "download"
    assert _safe_component("") == "download"


def test_safe_subpath_strips_traversal_keeps_nesting():
    assert _safe_subpath("acme-game/Assets/Sub") == "acme-game/Assets/Sub"
    assert _safe_subpath("a/../../b") == "a/b"
    assert _safe_subpath("//..//") == ""


# --------------------------------------------------------------------------- #
# resolve_archive_url must read the 302 Location (httpx raises on 3xx — the bug
# that made every repo download 502 with "GitHub returned 302")
# --------------------------------------------------------------------------- #
async def test_resolve_archive_url_reads_302_location():
    import httpx

    from packages.core.settings import Settings
    from packages.github.client import GitHubClient
    from packages.github.ratelimit import RateLimiter

    gh = GitHubClient(
        settings=Settings(gh_pat="x", _env_file=None),
        limiter=RateLimiter(search_rpm=30, core_rpm=60, graphql_rpm=60),
    )
    codeload = "https://codeload.github.com/acme/game/legacy.zip/refs/heads/main"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"Location": codeload})
    )
    await gh._http.aclose()
    gh._http = httpx.AsyncClient(transport=transport)
    try:
        assert await gh.resolve_archive_url("acme", "game") == codeload
    finally:
        await gh.aclose()


async def test_429_without_headers_parks_bucket():
    """Secondary rate limits can arrive with NO reset headers; the client must park the
    bucket anyway (default 60s) instead of hammering an already-throttled endpoint —
    this is what turned one 429 into a terminal job error for a live search."""
    import time

    import httpx

    from packages.core.settings import Settings
    from packages.github.client import GitHubClient
    from packages.github.ratelimit import RateLimiter

    gh = GitHubClient(
        settings=Settings(gh_pat="x", _env_file=None),
        limiter=RateLimiter(search_rpm=30, core_rpm=60, graphql_rpm=60),
    )
    parked: list[float] = []
    gh.limiter.note_server_reset = lambda bucket, ts: parked.append(ts)  # type: ignore[method-assign]
    # The real-world shape that stalled live searches: HTML body (no "rate limit"
    # text) AND a full remaining quota — only the 429 status itself says the truth.
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            429, text="<!DOCTYPE html><html>slow down</html>",
            headers={"x-ratelimit-remaining": "10"},
        )
    )
    await gh._http.aclose()
    gh._http = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await gh.get_json("/rate-limited", use_etag=False)
    finally:
        await gh.aclose()
    assert parked, "429 without headers must still park the bucket"
    assert all(ts > time.time() + 30 for ts in parked)  # meaningful cool-down


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeGH:
    def __init__(self, *, contents=None, archive_url="https://codeload/x.zip", tree=None):
        self._contents = contents or {}
        self._archive_url = archive_url
        self._tree = tree or []

    async def get_contents(self, owner, repo, path="", ref=None):
        return self._contents[path]

    async def resolve_archive_url(self, owner, repo, ref=None):
        return self._archive_url

    async def get_recursive_tree(self, owner, repo, ref=None):
        return self._tree, False, "commitsha"

    async def raw_auth_header(self):
        return None


class FakeManager:
    def __init__(self, *, available=True, views=None, download_dir=None, free=10**12):
        self._available = available
        self._views = views or {}
        self._next_gid = 0
        self._free = free
        self.download_dir = download_dir or Path("/nonexistent-fake-downloads")
        self.enqueued: list[tuple[str, str, str | None]] = []
        self.actions: list[tuple[str, str]] = []

    @property
    def available(self):
        return self._available

    def free_bytes(self):
        return self._free

    def enqueue(self, url, *, subdir="", filename=None, headers=None):
        self.enqueued.append((url, subdir, filename))
        self._next_gid += 1
        gid = f"gid{self._next_gid}"
        # Real aria2 tracks a GID from the moment add_uri returns — mirror that, or
        # the reconciler would treat brand-new downloads as purged-and-complete.
        self._views[gid] = _view(gid, status="waiting", total=0, done=0, speed=0)
        return gid

    def views_by_gid(self):
        return dict(self._views)

    def pause(self, gid):
        self.actions.append(("pause", gid))

    def resume(self, gid):
        self.actions.append(("resume", gid))

    def cancel(self, gid):
        self.actions.append(("cancel", gid))


def _view(gid, *, status="active", total=100, done=50, speed=10, error=None):
    return DownloadView(
        gid=gid, name=gid, status=status, total_bytes=total, completed_bytes=done,
        speed_bytes=speed, progress=(done / total) if total else 0.0, error=error,
        path=None,
    )


@pytest.fixture
def app_with():
    from apps.api.main import app, get_client, get_downloads

    def _make(gh: FakeGH, mgr: FakeManager) -> TestClient:
        app.dependency_overrides[get_client] = lambda: gh
        app.dependency_overrides[get_downloads] = lambda: mgr
        return TestClient(app)

    yield _make
    from apps.api.main import app as _app

    _app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_enqueue_file_resolves_download_url(app_with):
    gh = FakeGH(contents={"src/a.py": {"download_url": "https://raw/src/a.py", "size": 9}})
    mgr = FakeManager()
    with app_with(gh, mgr) as c:
        resp = c.post("/downloads", json={
            "kind": "file", "owner": "acme", "repo": "game", "path": "src/a.py",
        })
    assert resp.status_code == 202
    body = resp.json()
    assert body["kind"] == "file" and body["total_bytes"] == 9
    assert mgr.enqueued == [("https://raw/src/a.py", "acme-game", "a.py")]


def test_enqueue_repo_resolves_archive_url(app_with):
    gh = FakeGH(archive_url="https://codeload.github.com/acme/game/legacy.zip/main")
    mgr = FakeManager()
    with app_with(gh, mgr) as c:
        resp = c.post("/downloads", json={"kind": "repo", "owner": "acme", "repo": "game"})
    assert resp.status_code == 202
    assert resp.json()["label"] == "acme/game (full repo .zip)"
    assert mgr.enqueued == [
        ("https://codeload.github.com/acme/game/legacy.zip/main", "", "game.zip")
    ]


def test_enqueue_folder_fans_out_raw_urls(app_with):
    tree = [
        {"path": "Assets/Sub Dir/A File.cs", "type": "blob", "size": 100},
        {"path": "Assets/b.cs", "type": "blob", "size": 50},
        {"path": "Other/c.cs", "type": "blob", "size": 7},
    ]
    gh = FakeGH(tree=tree)
    mgr = FakeManager()
    with app_with(gh, mgr) as c:
        resp = c.post("/downloads", json={
            "kind": "folder", "owner": "acme", "repo": "game", "path": "Assets",
        })
    assert resp.status_code == 202
    body = resp.json()
    assert body["kind"] == "folder"
    assert body["file_count"] == 2  # "Other" excluded
    assert body["total_bytes"] == 150
    # Raw URLs pinned to the commit, path segments percent-encoded, dirs mirrored.
    assert mgr.enqueued == [
        (
            "https://raw.githubusercontent.com/acme/game/commitsha/Assets/Sub%20Dir/A%20File.cs",
            "acme-game/Assets/Sub Dir",
            "A File.cs",
        ),
        (
            "https://raw.githubusercontent.com/acme/game/commitsha/Assets/b.cs",
            "acme-game/Assets",
            "b.cs",
        ),
    ]


def test_enqueue_folder_empty_404(app_with):
    with app_with(FakeGH(tree=[]), FakeManager()) as c:
        resp = c.post("/downloads", json={
            "kind": "folder", "owner": "a", "repo": "b", "path": "nope",
        })
    assert resp.status_code == 404


def test_enqueue_when_aria2_missing_returns_503(app_with):
    with app_with(FakeGH(), FakeManager(available=False)) as c:
        resp = c.post("/downloads", json={
            "kind": "repo", "owner": "a", "repo": "b",
        })
    assert resp.status_code == 503
    assert "aria2" in resp.json()["detail"].lower()


_FOLDER_TREE = [
    {"path": "F/a.bin", "type": "blob", "size": 100},
    {"path": "F/b.bin", "type": "blob", "size": 100},
]


def _write_dest(base: Path, rel: str, size: int = 1) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_history_persists_and_aggregates(app_with, tmp_path):
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(FakeGH(tree=_FOLDER_TREE), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]

        # One child mid-flight, one done → row aggregates to active, 150/200 bytes.
        mgr._views = {
            "gid1": _view("gid1", status="active", total=100, done=50, speed=10),
            "gid2": _view("gid2", status="complete", total=100, done=100, speed=0),
        }
        items = c.get("/downloads").json()["items"]
        row = next(i for i in items if i["id"] == did)
        assert row["status"] == "active"
        assert row["completed_bytes"] == 150 and row["total_bytes"] == 200
        assert row["speed_bytes"] == 10

        # Engine purged both GIDs, files present on disk without .aria2 sidecars →
        # verifiably complete.
        _write_dest(tmp_path, "o-r/F/a.bin", 100)
        _write_dest(tmp_path, "o-r/F/b.bin", 100)
        mgr._views = {}
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == did)
        assert row["status"] == "complete"
        assert row["completed_bytes"] == 200


def test_repo_download_gets_estimated_total(app_with, tmp_path):
    """Repo zips have no Content-Length, so the total is pre-estimated from the tree's
    blob sizes ('30 MB / ~370 MB' instead of 'size unknown') and replaced by the real
    total once the engine reports one."""
    gh = FakeGH(
        archive_url="https://codeload/x.zip",
        tree=[
            {"path": "a.bin", "type": "blob", "size": 300},
            {"path": "b/c.bin", "type": "blob", "size": 200},
            {"path": "b", "type": "tree"},  # non-blobs don't count
        ],
    )
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(gh, mgr) as c:
        body = c.post("/downloads", json={
            "kind": "repo", "owner": "o", "repo": "r",
        }).json()
        assert body["total_bytes"] == 500
        assert body["total_is_estimate"] is True

        # Mid-flight: engine still has no total; live bytes flow against the estimate.
        mgr._views = {"gid1": _view("gid1", status="active", total=0, done=250, speed=50)}
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == body["id"])
        assert row["completed_bytes"] == 250
        assert row["total_bytes"] == 500 and row["total_is_estimate"] is True
        assert 0.4 < row["progress"] < 0.6

        # Stream closed: engine now knows the REAL total → estimate replaced for good.
        mgr._views = {"gid1": _view("gid1", status="complete", total=450, done=450)}
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == body["id"])
        assert row["total_bytes"] == 450
        assert row["total_is_estimate"] is False
        assert row["progress"] == 1.0


def test_column_migration_adds_missing_columns():
    """Existing user DBs predate total_is_estimate; init_db must ALTER them in."""
    from sqlalchemy import text

    import packages.storage.db as db

    engine = db.get_engine()
    with engine.begin() as conn:
        # Recreate the downloads table in its OLD shape (no total_is_estimate).
        conn.execute(text("DROP TABLE downloads"))
        conn.execute(text(
            "CREATE TABLE downloads (id INTEGER PRIMARY KEY, kind VARCHAR, "
            "label VARCHAR, owner VARCHAR, repo VARCHAR, path VARCHAR, children TEXT, "
            "status VARCHAR, total_bytes INTEGER, completed_bytes INTEGER, error TEXT, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
    db._ensure_columns(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(downloads)"))}
    assert "total_is_estimate" in cols


def test_unknown_length_download_reports_live_bytes(app_with, tmp_path):
    """Codeload repo zips stream chunked with no Content-Length: aria2 reports total=0
    while completed grows. The row must surface those live bytes, not persist 0."""
    gh = FakeGH(archive_url="https://codeload/x.zip")
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(gh, mgr) as c:
        did = c.post("/downloads", json={
            "kind": "repo", "owner": "o", "repo": "r",
        }).json()["id"]
        mgr._views = {
            "gid1": _view("gid1", status="active", total=0, done=77_000_000, speed=1_500_000),
        }
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == did)
        assert row["status"] == "active"
        assert row["completed_bytes"] == 77_000_000  # live bytes, even with total 0
        assert row["total_bytes"] == 0
        assert row["speed_bytes"] == 1_500_000

        # Finished: aria2 finally knows the real total once the stream closed.
        mgr._views = {
            "gid1": _view("gid1", status="complete", total=1_000_000_000,
                          done=1_000_000_000, speed=0),
        }
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == did)
        assert row["status"] == "complete"
        assert row["total_bytes"] == 1_000_000_000


def test_lost_child_is_error_not_fake_complete(app_with, tmp_path):
    """Engine forgot a GID and the file is NOT on disk → honest error + retry hint."""
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(FakeGH(tree=_FOLDER_TREE), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]
        _write_dest(tmp_path, "o-r/F/a.bin", 100)  # only ONE of the two files landed
        mgr._views = {}
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == did)
        assert row["status"] == "error"
        assert "retry" in (row["error"] or "").lower()


def test_folder_retry_re_enqueues_only_missing(app_with, tmp_path):
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(FakeGH(tree=_FOLDER_TREE), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]
        _write_dest(tmp_path, "o-r/F/a.bin", 100)  # a.bin done, b.bin lost
        mgr._views = {}
        before = len(mgr.enqueued)

        resp = c.post(f"/downloads/{did}/retry")
        assert resp.status_code == 202
        # Exactly one re-enqueue — the missing b.bin, same commit-pinned URL.
        assert len(mgr.enqueued) == before + 1
        url, subdir, filename = mgr.enqueued[-1]
        assert filename == "b.bin" and "commitsha/F/b.bin" in url


def test_repo_retry_re_resolves_url(app_with, tmp_path):
    gh = FakeGH(archive_url="https://codeload/first.zip")
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(gh, mgr) as c:
        did = c.post("/downloads", json={
            "kind": "repo", "owner": "o", "repo": "r",
        }).json()["id"]
        gh._archive_url = "https://codeload/fresh.zip"  # old pre-signed URL expired
        resp = c.post(f"/downloads/{did}/retry")
        assert resp.status_code == 202
        assert mgr.enqueued[-1][0] == "https://codeload/fresh.zip"


def test_files_detail_endpoint(app_with, tmp_path):
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(FakeGH(tree=_FOLDER_TREE), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]
        mgr._views = {
            "gid1": _view("gid1", status="active", total=100, done=40),
            "gid2": _view("gid2", status="paused", total=100, done=10),
        }
        body = c.get(f"/downloads/{did}/files").json()
    assert body["id"] == did
    assert [(f["path"], f["status"], f["completed_bytes"]) for f in body["files"]] == [
        ("F/a.bin", "active", 40),
        ("F/b.bin", "paused", 10),
    ]


def test_clear_finished_removes_terminal_rows(app_with, tmp_path):
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(FakeGH(tree=_FOLDER_TREE), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]
        c.delete(f"/downloads/{did}")  # → removed (terminal)
        assert c.post("/downloads/clear-finished").json()["removed"] == 1
        assert all(i["id"] != did for i in c.get("/downloads").json()["items"])


def test_recent_searches_endpoint(app_with, tmp_path):
    import packages.storage.db as db
    from packages.storage.repositories import SqlSearchStore

    with db.session_scope() as s:
        for kw in ("first", "second"):
            SqlSearchStore(s).create(keyword=kw, normalized_query=kw, search_type="keyword")
    with app_with(FakeGH(), FakeManager(download_dir=tmp_path)) as c:
        body = c.get("/searches").json()
    assert [r["keyword"] for r in body["searches"]] == ["second", "first"]  # newest first
    assert body["searches"][0]["search_id"] > body["searches"][1]["search_id"]


def test_reveal_selects_file_in_finder(app_with, tmp_path, monkeypatch):
    import apps.api.main as main

    calls: list[list[str]] = []
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(main.sys, "platform", "darwin")

    gh = FakeGH(archive_url="https://codeload/x.zip")
    mgr = FakeManager(download_dir=tmp_path)
    with app_with(gh, mgr) as c:
        did = c.post("/downloads", json={
            "kind": "repo", "owner": "o", "repo": "r",
        }).json()["id"]
        _write_dest(tmp_path, "r.zip", 10)  # the finished zip on disk
        assert c.post(f"/downloads/{did}/reveal").status_code == 204
    assert calls == [["open", "-R", str(tmp_path / "r.zip")]]  # selected, not just opened


def test_reveal_missing_file_falls_back_to_downloads_root(app_with, tmp_path, monkeypatch):
    import apps.api.main as main

    calls: list[list[str]] = []
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    with app_with(FakeGH(archive_url="https://x/z.zip"), FakeManager(download_dir=tmp_path)) as c:
        did = c.post("/downloads", json={
            "kind": "repo", "owner": "o", "repo": "r",
        }).json()["id"]
        assert c.post(f"/downloads/{did}/reveal").status_code == 204  # nothing on disk yet
    assert calls == [["open", str(tmp_path)]]


def test_preflight_rejects_when_disk_full(app_with, tmp_path):
    gh = FakeGH(contents={"a.bin": {"download_url": "https://raw/a.bin", "size": 100}})
    mgr = FakeManager(download_dir=tmp_path, free=10)
    with app_with(gh, mgr) as c:
        resp = c.post("/downloads", json={
            "kind": "file", "owner": "o", "repo": "r", "path": "a.bin",
        })
    assert resp.status_code == 507
    assert "disk space" in resp.json()["detail"].lower()


def test_pause_resume_cancel_fan_out_by_id(app_with):
    tree = [
        {"path": "F/a.bin", "type": "blob", "size": 1},
        {"path": "F/b.bin", "type": "blob", "size": 1},
    ]
    mgr = FakeManager()
    with app_with(FakeGH(tree=tree), mgr) as c:
        did = c.post("/downloads", json={
            "kind": "folder", "owner": "o", "repo": "r", "path": "F",
        }).json()["id"]
        assert c.post(f"/downloads/{did}/pause").status_code == 204
        assert c.post(f"/downloads/{did}/resume").status_code == 204
        assert c.delete(f"/downloads/{did}").status_code == 204
        row = next(i for i in c.get("/downloads").json()["items"] if i["id"] == did)
        assert row["status"] == "removed"
    assert mgr.actions == [
        ("pause", "gid1"), ("pause", "gid2"),
        ("resume", "gid1"), ("resume", "gid2"),
        ("cancel", "gid1"), ("cancel", "gid2"),
    ]
