"""FastAPI app.

Three traffic types, deliberately separated:
- Read path (`/search`, `/facets`) queries the LOCAL index only — never GitHub — instant.
- Write path (`/index`) enqueues a discovery job; the background WorkerPool does the
  rate-limited GitHub indexing work.
- Inspection path (`/repo/...`) does on-demand LIVE GitHub reads (browse/preview/download)
  for a single repo the user is looking at — not from the index, not via the queue.

The inspection path shares the WorkerPool's single GitHubClient, so ONE RateLimiter
governs indexing + inspection together and they can't jointly exceed GitHub's limits.
See docs/DATA_MODEL_AND_PIPELINE.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from apps.api.schemas import (
    BlobResponse,
    BucketStatus,
    DetectResponse,
    DownloadFile,
    DownloadFilesResponse,
    DownloadItem,
    DownloadRequest,
    DownloadsResponse,
    FacetsResponse,
    IndexRequest,
    IndexResponse,
    RecentSearch,
    RecentSearchesResponse,
    ResultRow,
    SearchResponse,
    SizesResponse,
    StatusResponse,
    TreeEntry,
    TreeResponse,
)
from packages.core.enums import JobType, SearchType
from packages.core.query import detect as detect_query_text
from packages.core.query import normalize as normalize_query
from packages.core.settings import get_settings
from packages.downloads.history import (
    child_complete_on_disk,
    child_gids,
    create_download,
    get_download,
    list_files,
    mark_status,
    purge_finished,
    reconcile_and_list,
    replace_children,
)
from packages.downloads.manager import DownloadManager
from packages.github.client import GitHubClient
from packages.github.files import (
    MAX_FOLDER_FILES,
    MAX_FOLDER_ZIP_BYTES,
    decode_blob,
    folder_sizes,
    zip_files,
)
from packages.storage.db import init_db, session_scope
from packages.storage.orm import (
    AssetHistory,
    Job,
    Match,
    Repository,
    Search,
    SearchJob,
    SearchResult,
)
from packages.storage.repositories import (
    SqlSearchStore,
    apply_facet_filters,
    compute_facets,
)
from packages.indexer.queue import (
    enqueue,
    recover_orphaned_jobs,
    reconcile_search_readiness,
    repair_search_statuses,
)
from packages.indexer.worker import WorkerPool, build_client


def _setup_logging() -> None:
    """Route our loggers to the console AND data/debug.log. Uvicorn only configures its
    own loggers, so without this the pipeline's diagnostics go nowhere — and a stalled
    collection can't be traced (learned the hard way)."""
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    Path("data").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler("data/debug.log"),
    ]
    for h in handlers:
        h.setFormatter(fmt)
    for name in ("github", "indexer", "downloads"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            for h in handlers:
                logger.addHandler(h)


_setup_logging()


class _AppState:
    """Holds process-wide singletons so request handlers can reach them via dependencies
    (and tests can override those dependencies)."""

    gh: GitHubClient | None = None
    pool: WorkerPool | None = None
    downloads: DownloadManager | None = None


_state = _AppState()


def get_client() -> GitHubClient:
    if _state.gh is None:
        raise HTTPException(503, "GitHub client not initialised")
    return _state.gh


def get_downloads() -> DownloadManager:
    if _state.downloads is None:
        raise HTTPException(503, "download manager not initialised")
    return _state.downloads


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Crash/restart recovery, BEFORE workers start: requeue jobs orphaned in 'running',
    # flip searches whose discovery died terminally to 'error', and let any search whose
    # jobs all finished while we were down reach 'ready'.
    recover_orphaned_jobs()
    repair_search_statuses()
    reconcile_search_readiness()
    gh = build_client()
    _state.gh = gh
    _state.pool = WorkerPool(client=gh)
    await _state.pool.start()

    settings = get_settings()
    _state.downloads = DownloadManager(
        download_dir=settings.download_dir,
        rpc_port=settings.aria2_rpc_port,
        max_connections=settings.aria2_max_connections,
    )
    if settings.downloads_autostart:
        _state.downloads.start()  # no-op if aria2c isn't installed
    try:
        yield
    finally:
        _state.downloads.stop()
        await _state.pool.stop()
        await gh.aclose()
        _state.gh = None


app = FastAPI(title="GitHub Code Explorer", lifespan=lifespan)


@dataclass
class ResultFilters:
    """The filter state shared by /search (filters results) and /facets (cross-filters
    counts). Declared once as a dependency so the two endpoints accept identical params
    and can never fall out of sync. Group names match FACET_COLUMNS / the /facets keys.
    """

    collapse_forks: bool = True
    filters: dict[str, list[str]] = field(default_factory=dict)


def result_filters(
    collapse_forks: bool = Query(True),
    languages: list[str] = Query(default=[]),
    extensions: list[str] = Query(default=[]),
    path_prefixes: list[str] = Query(default=[]),
    owners: list[str] = Query(default=[]),
    licenses: list[str] = Query(default=[]),
) -> ResultFilters:
    return ResultFilters(
        collapse_forks=collapse_forks,
        filters={
            "languages": languages,
            "extensions": extensions,
            "path_prefixes": path_prefixes,
            "owners": owners,
            "licenses": licenses,
        },
    )


@app.get("/detect", response_model=DetectResponse)
def detect_query(q: str = Query("")) -> DetectResponse:
    """What would we do with this input? Powers the live chip under the search box, so
    the auto-detection is always visible *before* you press Index — and is the same code
    path `/index` uses, so the preview can never lie about what will run."""
    d = detect_query_text(q)
    return DetectResponse(
        search_type=str(d.search_type),
        query=d.query,
        normalized_query=d.normalized,
        explanation=d.explanation,
        repo=d.repo,
        path=d.path,
        ref=d.ref,
    )


@app.post("/index", response_model=IndexResponse)
def index(req: IndexRequest) -> IndexResponse:
    """Kick off (or reuse) indexing for a keyword. Returns immediately; work is
    backgrounded. Re-submitting a query that is already indexed or in flight returns
    the existing search — no duplicate rows, no wasted search-rate budget.

    `search_type` defaults to AUTO: the input is classified (URL, path, filename,
    extension, phrase, raw GitHub syntax, or plain keyword) and the resolved type comes
    back on the response so the UI can show what it decided.
    """
    resolved = normalize_query(req.keyword, req.search_type)
    if not resolved.normalized.strip():
        raise HTTPException(400, "empty search")
    if resolved.search_type == SearchType.REPO:
        # A bare repo link is not a search — there is nothing to collect and indexing
        # a whole repo through code search would burn the budget for no reason. Hand the
        # coordinates back and let the UI open the inspector.
        raise HTTPException(
            409,
            f"'{resolved.repo}' is a repository, not a search. Open it in the inspector.",
        )
    normalized = resolved.normalized
    keyword = resolved.query or req.keyword
    search_type = resolved.search_type
    repair_id: int | None = None
    with session_scope() as s:
        existing = s.scalar(
            select(Search)
            .where(
                # GitHub code search is case-insensitive — "exoa" and "Exoa" are the
                # same query and must share one search instead of racing in parallel.
                func.lower(Search.normalized_query) == normalized.lower(),
                Search.search_type == str(search_type),
                Search.status.in_(["pending", "discovering", "enriching", "ready"]),
            )
            .order_by(Search.id.desc())
        )
        if existing is not None and existing.status == "ready" and existing.total_matches == 0:
            # A finished-but-empty search is usually the fossil of a GitHub hiccup (a
            # degraded response, a rate-limit wall). Reusing it would make every retry
            # instantly "return" the same nothing, so start a fresh one instead.
            existing = None
        if existing is not None:
            if existing.status == "pending":
                # Self-repair: a pending search whose discovery job is missing or not
                # live would otherwise never start. A job that is anything but
                # queued/running here is a stale leftover from a reused search id —
                # including 'done' (its old run belonged to a DIFFERENT, deleted
                # search and may carry that search's query in its payload).
                job = s.scalar(
                    select(Job).where(Job.dedup_key == f"discovery:{existing.id}")
                )
                if job is None or job.status not in ("queued", "running"):
                    if job is not None:
                        s.delete(job)
                        s.flush()
                    repair_id = existing.id
            if repair_id is None:
                return IndexResponse(
                    search_id=existing.id, status=existing.status,
                    search_type=str(search_type), normalized_query=normalized,
                    explanation=resolved.explanation,
                )
            search_id, keyword_normalized = existing.id, existing.normalized_query
        else:
            search = SqlSearchStore(s).create(
                keyword=keyword, normalized_query=normalized,
                search_type=str(search_type),
            )
            search_id, keyword_normalized = search.id, normalized
    enqueue(
        JobType.DISCOVERY,
        f"discovery:{search_id}",
        {"search_id": search_id, "normalized_query": keyword_normalized},
    )
    return IndexResponse(
        search_id=search_id, status="pending", search_type=str(search_type),
        normalized_query=normalized, explanation=resolved.explanation,
    )


@app.get("/searches", response_model=RecentSearchesResponse)
def recent_searches(limit: int = Query(20, le=100)) -> RecentSearchesResponse:
    """Recent searches, newest first — the home screen's 'pick up where you left off'
    list, so closing the tab or refreshing never strands the user."""
    with session_scope() as s:
        rows = s.scalars(select(Search).order_by(Search.id.desc()).limit(limit)).all()
        return RecentSearchesResponse(searches=[
            RecentSearch(
                search_id=r.id, keyword=r.keyword, search_type=r.search_type,
                status=r.status, truncated=r.truncated, total_matches=r.total_matches,
                reported_matches=r.reported_matches, note=r.note,
                created_at=r.created_at,
            )
            for r in rows
        ])


@app.delete("/searches/{search_id}", status_code=204)
def delete_search(search_id: int) -> Response:
    """Delete (or cancel) a search. Its result links and readiness links go too; the
    global repo/match cache stays (other searches share it). A queued discovery job is
    skipped; a RUNNING one notices the missing row at its next progress heartbeat and
    aborts — so this doubles as 'cancel' for a long discovery."""
    from sqlalchemy import delete as sql_delete

    with session_scope() as s:
        search = s.get(Search, search_id)
        if search is None:
            raise HTTPException(404, "search not found")
        # Delete the discovery job ROW (not just mark it skipped): SQLite reuses row
        # ids, so a lingering `discovery:{id}` job would block the enqueue of any
        # future search that gets this id — leaving it 'pending' forever.
        s.execute(sql_delete(Job).where(Job.dedup_key == f"discovery:{search_id}"))
        s.execute(sql_delete(SearchResult).where(SearchResult.search_id == search_id))
        s.execute(sql_delete(SearchJob).where(SearchJob.search_id == search_id))
        s.delete(search)
    return Response(status_code=204)


# (entity, attribute, descending). "history" resolves to the AssetHistory alias below,
# since the same table is left-joined under an alias in the query.
_SORTS = {
    "asset_added_desc": ("history", "first_appeared_at", True),
    "asset_added_asc": ("history", "first_appeared_at", False),
    "repo_created_desc": ("repo", "created_at", True),
    "repo_updated_desc": ("repo", "pushed_at", True),
    "stars_desc": ("repo", "stars", True),
    "forks_desc": ("repo", "forks", True),
    "relevance": ("result", "rank", False),
}


@app.get("/search/{search_id}", response_model=SearchResponse)
def get_search(
    search_id: int,
    sort: str = Query("asset_added_desc"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    rf: ResultFilters = Depends(result_filters),
) -> SearchResponse:
    """The headline read: results sortable by when the asset FIRST APPEARED — the thing
    GitHub Code Search cannot do. Served entirely from the local index.

    Facet filters (languages/extensions/path_prefixes/owners/licenses) are applied
    server-side so filtering works across the whole result set, not just the page the
    client has fetched. Each is a repeatable query param; the group names match the keys
    returned by /facets exactly (see FACET_COLUMNS).
    """
    if sort not in _SORTS:
        raise HTTPException(400, f"unknown sort '{sort}'")
    sort_entity, sort_attr, descending = _SORTS[sort]

    with session_scope() as s:
        search = s.get(Search, search_id)
        if search is None:
            raise HTTPException(404, "search not found")

        hist = aliased(AssetHistory)
        column = getattr(
            {"history": hist, "repo": Repository, "result": SearchResult}[sort_entity],
            sort_attr,
        )
        stmt = (
            select(Repository, Match, SearchResult.rank, hist)
            .join(Match, Match.id == SearchResult.match_id)
            .join(Repository, Repository.id == Match.repo_id)
            .join(
                hist,
                (hist.repo_id == Match.repo_id)
                & (hist.path == Match.path_prefix)
                & (hist.method == "api-approx"),
                isouter=True,
            )
            .where(SearchResult.search_id == search_id)
        )
        if rf.collapse_forks:
            stmt = stmt.where(Repository.is_fork.is_(False))
        stmt = apply_facet_filters(stmt, rf.filters)

        order_col = column.desc() if descending else column.asc()
        stmt = stmt.order_by(order_col.nulls_last()).limit(limit).offset(offset)

        rows = []
        for repo, match, rank, history in s.execute(stmt).all():
            rows.append(
                ResultRow(
                    repo_full_name=repo.full_name,
                    owner=repo.owner,
                    stars=repo.stars,
                    license_spdx=repo.license_spdx,
                    path=match.path,
                    filename=match.filename,
                    extension=match.extension,
                    path_prefix=match.path_prefix,
                    detected_language=match.detected_language,
                    snippet=match.snippet,
                    repo_created_at=repo.created_at,
                    repo_pushed_at=repo.pushed_at,
                    asset_first_appeared_at=history.first_appeared_at if history else None,
                    history_method=history.method if history else None,
                    github_url=f"https://github.com/{repo.full_name}/blob/"
                    f"{repo.default_branch or 'HEAD'}/{match.path}",
                )
            )
        return SearchResponse(
            search_id=search_id,
            keyword=search.keyword,
            search_type=search.search_type,
            normalized_query=search.normalized_query,
            status=search.status,
            truncated=search.truncated,
            sampled=search.sampled,
            total_matches=search.total_matches,
            reported_matches=search.reported_matches,
            note=search.note,
            results=rows,
        )


@app.get("/facets/{search_id}", response_model=FacetsResponse)
def get_facets(
    search_id: int,
    rf: ResultFilters = Depends(result_filters),
) -> FacetsResponse:
    """Auto-discovered filters for the sidebar. Counts are cross-filtered by the active
    selection (each group excludes its own filter — see compute_facets), so they narrow
    as you filter on other groups while a filtered group still lists all its options.
    """
    with session_scope() as s:
        if s.get(Search, search_id) is None:
            raise HTTPException(404, "search not found")
        facets = compute_facets(
            s, search_id, filters=rf.filters, collapse_forks=rf.collapse_forks
        )
    return FacetsResponse(search_id=search_id, facets=facets)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Shutdown — this is a desktop-style local app, so quitting it should be a button,
# not `pkill uvicorn` in a terminal the user may not even have open.
# --------------------------------------------------------------------------- #
_WEB_PROCESS_MARKERS = ("vite", "npm", "node")


def _command_of(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        return ""


def _children_of(pid: int) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return []
    return [int(line) for line in out.split() if line.isdigit()]


def _web_dev_server_pid() -> int | None:
    """The PID `scripts/start.sh` recorded for the dev server, if that process still
    looks like the dev server. Non-destructive, so the endpoint can *report* what it is
    about to stop before stopping it. A recycled PID must never be signalled, so the
    recorded number alone is never enough — the command line has to match too.
    """
    pid_file = Path(".run/web.pid")
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None
    cmd = _command_of(pid)
    if not cmd or not any(marker in cmd for marker in _WEB_PROCESS_MARKERS):
        return None  # gone, or the PID now belongs to something else entirely
    return pid


def _stop_web_dev_server() -> str | None:
    """Terminate the dev server. Returns a description of what was stopped, or None.

    The recorded PID is `npm run dev`; Vite itself is its child and owns the port.
    Whether npm forwards SIGTERM is version-dependent, so we signal the child directly
    too — a surviving Vite would serve a UI with no backend behind it, which is exactly
    the "looks fine, does nothing" state this app tries never to present.
    """
    pid = _web_dev_server_pid()
    if pid is None:
        return None

    # Children first, so npm can't respawn or outlive them; each is verified the same way.
    targets = [c for c in _children_of(pid)
               if any(m in _command_of(c) for m in _WEB_PROCESS_MARKERS)]
    targets.append(pid)

    stopped = []
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
            stopped.append(target)
        except (ProcessLookupError, PermissionError):
            continue
    if not stopped:
        return None
    Path(".run/web.pid").unlink(missing_ok=True)
    return f"web dev server (pid {pid})"


@app.post("/shutdown")
async def shutdown() -> dict[str, object]:
    """Quit the whole app. Responds first, then exits — so the UI can render a clean
    'stopped' screen instead of a failed fetch.

    SIGTERM (not `os._exit`) so uvicorn runs the lifespan teardown: aria2 stops, the
    worker pool drains, and the GitHub client closes. In-flight downloads survive as
    history rows and resume on the next launch.

    Ordering matters: the dev server is the proxy the browser reaches us THROUGH, so
    killing it before the response flushes guarantees the page never sees the reply.
    Everything destructive therefore happens in the deferred task, after the response.
    """
    web_pid = _web_dev_server_pid()

    async def _exit_after_response() -> None:
        await asyncio.sleep(0.3)  # let the HTTP response flush first
        _stop_web_dev_server()
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.get_running_loop().create_task(_exit_after_response())
    stopping = ["backend"] + ([f"web dev server (pid {web_pid})"] if web_pid else [])
    return {"status": "stopping", "stopping": stopping}


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    """Is anything actually wrong? Credentials, GitHub's mood, and our own rate-limit
    wait, in one poll. This is what turns 'it just keeps loading' into a sentence: the
    UI shows a banner the moment the token is rejected or code search starts refusing to
    return results, instead of leaving a spinner to imply progress.
    """
    settings = get_settings()
    gh = _state.gh
    gh_health = gh.health if gh is not None else None
    buckets = (
        {
            name: BucketStatus(**vars(state))
            for name, state in gh.limiter.snapshot().items()
        }
        if gh is not None
        else {}
    )

    if settings.has_app_auth:
        credential = "github-app"
    elif settings.gh_pat:
        credential = "personal-access-token"
    else:
        credential = "none"

    return StatusResponse(
        credential=credential,
        credential_ok=credential != "none",
        token_expires_at=gh_health.token_expires_at if gh_health else None,
        search_limit=gh_health.search_limit if gh_health else None,
        search_remaining=gh_health.search_remaining if gh_health else None,
        search_reset_in=(
            max(0, int(gh_health.search_reset_epoch - time.time()))
            if gh_health and gh_health.search_reset_epoch
            else None
        ),
        github_degraded=bool(gh_health and gh_health.degraded_since),
        last_error=gh_health.last_error if gh_health else None,
        buckets=buckets,
        message=_status_message(credential, gh_health),
    )


def _status_message(credential: str, gh_health) -> str | None:
    """The single sentence the UI shows. None when there is nothing to say — silence is
    the correct output for a healthy system."""
    if credential == "none":
        return (
            "No GitHub credentials configured. Set GH_PAT (or the GH_APP_* values) in "
            ".env and restart the backend — code search requires authentication."
        )
    if gh_health is None:
        return "Starting up — the GitHub client is not ready yet."
    if gh_health.degraded_since:
        return (
            "GitHub's code search is reporting matches but returning none. That is a "
            "problem on their side; searches will stay empty until it clears. Retry in "
            "a few minutes."
        )
    if gh_health.search_remaining == 0:
        return (
            "GitHub's code-search quota (10 requests/minute) is spent. Collection "
            "resumes automatically when it resets."
        )
    return gh_health.last_error


# --------------------------------------------------------------------------- #
# Inspection path — on-demand LIVE GitHub reads for one repo the user opened.
# --------------------------------------------------------------------------- #
def _guard(owner: str, repo: str) -> None:
    """Reject path-traversal / injection in the repo coordinates before we build URLs."""
    for part in (owner, repo):
        if not part or "/" in part or ".." in part:
            raise HTTPException(400, "invalid owner/repo")


def _github_error(exc: httpx.HTTPStatusError) -> HTTPException:
    code = exc.response.status_code if exc.response is not None else 502
    detail = "repo or path not found" if code == 404 else f"GitHub returned {code}"
    return HTTPException(code if code in (403, 404, 422) else 502, detail)


@app.get("/repo/{owner}/{repo}/contents", response_model=TreeResponse)
async def repo_contents(
    owner: str,
    repo: str,
    path: str = Query(""),
    ref: str | None = Query(None),
    gh: GitHubClient = Depends(get_client),
) -> TreeResponse:
    """Browse one directory of a repo (lazy — the UI fetches a folder as it's opened).
    Directories sort before files, each alphabetical."""
    _guard(owner, repo)
    try:
        data = await gh.get_contents(owner, repo, path, ref)
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc
    if isinstance(data, dict):
        raise HTTPException(400, "path is a file, not a directory — use /blob or /file")

    entries = [
        TreeEntry(
            name=e.get("name", ""),
            path=e.get("path", ""),
            type="dir" if e.get("type") == "dir" else "file",
            size=e.get("size"),
            sha=e.get("sha"),
        )
        for e in data
    ]
    entries.sort(key=lambda e: (e.type != "dir", e.name.lower()))
    return TreeResponse(owner=owner, repo=repo, ref=ref, path=path, entries=entries)


@app.get("/repo/{owner}/{repo}/sizes", response_model=SizesResponse)
async def repo_sizes(
    owner: str,
    repo: str,
    ref: str | None = Query(None),
    gh: GitHubClient = Depends(get_client),
) -> SizesResponse:
    """Total size of every folder in the repo, computed from one recursive tree fetch.
    The UI loads this once per repo and annotates directory rows at any depth (the
    contents API only ever reports 0 for a directory's own size)."""
    _guard(owner, repo)
    try:
        tree, truncated, _commit = await gh.get_recursive_tree(owner, repo, ref)
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc
    return SizesResponse(
        owner=owner, repo=repo, ref=ref, truncated=truncated, sizes=folder_sizes(tree)
    )


@app.get("/repo/{owner}/{repo}/blob", response_model=BlobResponse)
async def repo_blob(
    owner: str,
    repo: str,
    path: str = Query(...),
    ref: str | None = Query(None),
    gh: GitHubClient = Depends(get_client),
) -> BlobResponse:
    """Preview a single file's text (base64-decoded). Binary / oversized files come back
    with an `encoding` flag and no text so the UI offers a download instead."""
    _guard(owner, repo)
    try:
        data = await gh.get_contents(owner, repo, path, ref)
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc
    if isinstance(data, list):
        raise HTTPException(400, "path is a directory — use /contents")
    encoding, text = decode_blob(data)
    return BlobResponse(
        owner=owner, repo=repo, path=path,
        size=data.get("size", 0), encoding=encoding, text=text,
    )


@app.get("/repo/{owner}/{repo}/file")
async def repo_file_download(
    owner: str,
    repo: str,
    path: str = Query(...),
    ref: str | None = Query(None),
    gh: GitHubClient = Depends(get_client),
) -> Response:
    """Download a single file as an attachment."""
    _guard(owner, repo)
    try:
        content = await gh.get_raw_file(owner, repo, path, ref)
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc
    filename = path.rstrip("/").rsplit("/", 1)[-1] or repo
    return Response(
        content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/repo/{owner}/{repo}/archive")
async def repo_archive(
    owner: str,
    repo: str,
    path: str = Query(""),
    ref: str | None = Query(None),
    gh: GitHubClient = Depends(get_client),
) -> Response:
    """Download a zip of the whole repo, or (with `path`) just one folder.

    Whole repo: streamed straight through from GitHub's zipball — no server buffering.
    Folder: enumerates ONLY that folder's files from the tree and zips them (cost is
    proportional to the folder, never the whole repo). Capped — oversized folders return
    413 so the UI can steer the user to a full download or git clone.
    """
    _guard(owner, repo)
    folder = path.strip("/")

    if folder:
        try:
            tree, _, _commit = await gh.get_recursive_tree(owner, repo, ref)
        except httpx.HTTPStatusError as exc:
            raise _github_error(exc) from exc

        prefix = f"{folder}/"
        blobs = [
            e for e in tree
            if e.get("type") == "blob" and e.get("path", "").startswith(prefix)
        ]
        if not blobs:
            raise HTTPException(404, "folder not found or empty")

        total = sum(e.get("size") or 0 for e in blobs)
        if len(blobs) > MAX_FOLDER_FILES or total > MAX_FOLDER_ZIP_BYTES:
            raise HTTPException(
                413,
                f"This folder is too large to zip on the fly "
                f"({len(blobs)} files, {total // (1024 * 1024)} MB). "
                f"Download the whole repo or use git clone instead.",
            )

        try:
            fetched = [
                (e["path"], await gh.get_raw_file(owner, repo, e["path"], ref))
                for e in blobs
            ]
        except httpx.HTTPStatusError as exc:
            raise _github_error(exc) from exc

        fname = f"{repo}-{folder.replace('/', '-')}.zip"
        return Response(
            zip_files(fetched),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    async def _stream():
        async with gh.stream_zipball(owner, repo, ref) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{repo}.zip"'},
    )


# --------------------------------------------------------------------------- #
# Download manager — backend-driven downloads to a local folder (aria2), so the
# browser's downloader is bypassed entirely. The endpoint resolves GitHub URLs;
# the manager owns aria2; SQLite `downloads` rows are the durable history the
# UI polls, aggregating a folder's many-file fan-out into one row.
# --------------------------------------------------------------------------- #
MAX_FOLDER_DOWNLOAD_FILES = 2000


def _require_engine(dl: DownloadManager) -> None:
    if not dl.available:
        raise HTTPException(
            503,
            "The download manager needs aria2. Install it with `brew install aria2`, "
            "then restart the backend.",
        )


def _preflight_disk(dl: DownloadManager, total_bytes: int) -> None:
    free = dl.free_bytes()
    if free is not None and total_bytes and total_bytes > free:
        raise HTTPException(
            507,
            f"Not enough disk space: this download needs "
            f"{total_bytes // (1024 * 1024)} MB but only "
            f"{free // (1024 * 1024)} MB is free.",
        )


async def _estimate_repo_zip_bytes(
    gh: GitHubClient, owner: str, repo: str, ref: str | None
) -> int:
    """Estimated zipball size = sum of every blob at HEAD. GitHub streams archives with
    no Content-Length, so this is what lets the UI show '30 MB / ~370 MB' + an ETA
    instead of 'size unknown'. Compression makes it an upper-ish bound; the real total
    replaces it when the engine reports one. Best-effort — 0 means unknown."""
    try:
        tree, _truncated, _commit = await gh.get_recursive_tree(owner, repo, ref)
    except httpx.HTTPStatusError:
        return 0
    return sum(e.get("size") or 0 for e in tree if e.get("type") == "blob")


async def _resolve_file(gh: GitHubClient, owner: str, repo: str, path: str,
                        ref: str | None) -> tuple[str, str, int]:
    """→ (pre-signed url, filename, size). Re-run on retry: pre-signed URLs expire."""
    data = await gh.get_contents(owner, repo, path, ref)
    if isinstance(data, list):
        raise HTTPException(400, "path is a directory, not a file")
    url = data.get("download_url")
    if not url:
        raise HTTPException(422, "no direct download URL for this file")
    return url, path.rstrip("/").rsplit("/", 1)[-1], int(data.get("size") or 0)


def _folder_children(owner: str, repo: str, commit: str | None,
                     blobs: list[dict]) -> list[dict]:
    """Child specs for a folder fan-out: commit-pinned raw URL + mirrored dest path."""
    children = []
    for e in blobs:
        rel = e["path"]
        children.append({
            "path": rel,
            "size": e.get("size") or 0,
            "url": (
                f"https://raw.githubusercontent.com/{owner}/{repo}/"
                f"{commit}/{quote(rel, safe='/')}"
            ),
            "dest": f"{owner}-{repo}/{rel}",
        })
    return children


def _enqueue_child(dl: DownloadManager, child: dict,
                   headers: list[str] | None) -> dict:
    """Hand one child spec to aria2 and record the resulting GID on it."""
    dest = child["dest"]
    parent, _, filename = dest.rpartition("/")
    child["gid"] = dl.enqueue(
        child["url"], subdir=parent, filename=filename, headers=headers
    )
    return child


@app.post("/downloads", response_model=DownloadItem, status_code=202)
async def enqueue_download(
    req: DownloadRequest,
    gh: GitHubClient = Depends(get_client),
    dl: DownloadManager = Depends(get_downloads),
) -> DownloadItem:
    """Queue a file, a folder (parallel per-file fan-out), or a whole repo for download.
    We resolve final (pre-signed if private) GitHub URLs here and hand the engine ready
    URLs — never a redirect that would forward the token across hosts."""
    _guard(req.owner, req.repo)
    _require_engine(dl)

    try:
        if req.kind == "file":
            if not req.path:
                raise HTTPException(400, "path is required for a file download")
            url, filename, size = await _resolve_file(
                gh, req.owner, req.repo, req.path, req.ref
            )
            _preflight_disk(dl, size)
            dest = f"{req.owner}-{req.repo}/{filename}"
            child = _enqueue_child(
                dl, {"path": req.path, "size": size, "url": url, "dest": dest}, None
            )
            download_id = create_download(
                kind="file", label=f"{req.owner}/{req.repo}: {filename}",
                owner=req.owner, repo=req.repo, path=req.path,
                children=[child], total_bytes=size,
            )

        elif req.kind == "folder":
            if not req.path:
                raise HTTPException(400, "path is required for a folder download")
            tree, _truncated, commit = await gh.get_recursive_tree(
                req.owner, req.repo, req.ref
            )
            prefix = req.path.strip("/") + "/"
            blobs = [
                e for e in tree
                if e.get("type") == "blob" and e.get("path", "").startswith(prefix)
            ]
            if not blobs:
                raise HTTPException(404, "folder not found or empty")
            if len(blobs) > MAX_FOLDER_DOWNLOAD_FILES:
                raise HTTPException(
                    413,
                    f"This folder has {len(blobs)} files (limit "
                    f"{MAX_FOLDER_DOWNLOAD_FILES}). Download the whole repo instead.",
                )
            children = _folder_children(req.owner, req.repo, commit, blobs)
            _preflight_disk(dl, sum(c["size"] for c in children))
            auth = await gh.raw_auth_header()
            headers = [auth] if auth else None
            children = [_enqueue_child(dl, c, headers) for c in children]
            download_id = create_download(
                kind="folder",
                label=f"{req.owner}/{req.repo}: {req.path.strip('/')}/ "
                f"({len(children)} files)",
                owner=req.owner, repo=req.repo, path=req.path,
                children=children,
                total_bytes=sum(c["size"] for c in children),
            )

        elif req.kind == "repo":
            url = await gh.resolve_archive_url(req.owner, req.repo, req.ref)
            estimate = await _estimate_repo_zip_bytes(gh, req.owner, req.repo, req.ref)
            _preflight_disk(dl, estimate)
            child = _enqueue_child(
                dl, {"path": None, "size": 0, "url": url, "dest": f"{req.repo}.zip"},
                None,
            )
            download_id = create_download(
                kind="repo", label=f"{req.owner}/{req.repo} (full repo .zip)",
                owner=req.owner, repo=req.repo, path=None, children=[child],
                total_bytes=estimate, total_is_estimate=bool(estimate),
            )
        else:
            raise HTTPException(400, f"unknown download kind '{req.kind}'")
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc

    item = next(
        (i for i in reconcile_and_list(dl) if i["id"] == download_id), None
    )
    if item is None:  # can't happen (we just created it), but never 500 on a race
        raise HTTPException(500, "download row vanished")
    return DownloadItem(**item)


@app.post("/downloads/{download_id}/retry", response_model=DownloadItem, status_code=202)
async def retry_download(
    download_id: int,
    gh: GitHubClient = Depends(get_client),
    dl: DownloadManager = Depends(get_downloads),
) -> DownloadItem:
    """Re-run a failed download. Files and repos re-resolve their URL from scratch
    (pre-signed/codeload URLs expire); folders re-enqueue ONLY the children that aren't
    verifiably complete on disk, so a 500-file folder that died at 480 fetches 20."""
    _require_engine(dl)
    row = get_download(download_id)
    if row is None:
        raise HTTPException(404, "download not found")

    try:
        if row["kind"] == "file":
            url, filename, size = await _resolve_file(
                gh, row["owner"], row["repo"], row["path"], None
            )
            dest = f"{row['owner']}-{row['repo']}/{filename}"
            child = _enqueue_child(
                dl, {"path": row["path"], "size": size, "url": url, "dest": dest}, None
            )
            replace_children(download_id, [child], total_bytes=size)

        elif row["kind"] == "repo":
            url = await gh.resolve_archive_url(row["owner"], row["repo"], None)
            estimate = await _estimate_repo_zip_bytes(gh, row["owner"], row["repo"], None)
            child = _enqueue_child(
                dl,
                {"path": None, "size": 0, "url": url, "dest": f"{row['repo']}.zip"},
                None,
            )
            replace_children(
                download_id, [child],
                total_bytes=estimate, total_is_estimate=bool(estimate),
            )

        else:  # folder
            children = row["children"]
            if any(not c.get("url") or not c.get("dest") for c in children):
                raise HTTPException(
                    409, "this download predates retry support — start a new one"
                )
            auth = await gh.raw_auth_header()
            headers = [auth] if auth else None
            redone = 0
            for child in children:
                if child_complete_on_disk(child, dl.download_dir):
                    continue  # keep as-is; reconcile counts it complete from disk
                _enqueue_child(dl, child, headers)  # fresh gid, same pinned URL
                redone += 1
            if redone == 0:
                mark_status(download_id, "complete")
            else:
                replace_children(
                    download_id, children,
                    total_bytes=sum(c["size"] for c in children),
                )
    except httpx.HTTPStatusError as exc:
        raise _github_error(exc) from exc

    item = next((i for i in reconcile_and_list(dl) if i["id"] == download_id), None)
    if item is None:
        raise HTTPException(500, "download row vanished")
    return DownloadItem(**item)


@app.get("/downloads/{download_id}/files", response_model=DownloadFilesResponse)
def download_files(
    download_id: int, dl: DownloadManager = Depends(get_downloads)
) -> DownloadFilesResponse:
    """Per-file status inside one download — the expandable detail view."""
    files = list_files(download_id, dl)
    if files is None:
        raise HTTPException(404, "download not found")
    return DownloadFilesResponse(
        id=download_id, files=[DownloadFile(**f) for f in files]
    )


@app.post("/downloads/clear-finished", status_code=200)
def clear_finished() -> dict[str, int]:
    """Remove finished/errored/cancelled rows from history."""
    return {"removed": purge_finished()}


@app.post("/downloads/{download_id}/reveal", status_code=204)
def reveal_download(
    download_id: int, dl: DownloadManager = Depends(get_downloads)
) -> Response:
    """Open the download's location in Finder ('Show in folder', like a browser).
    Files/repo zips are selected in their folder; folder downloads open their root.
    Localhost-only tool, and the path comes from OUR stored dest — never user input.
    """
    row = get_download(download_id)
    if row is None:
        raise HTTPException(404, "download not found")
    if sys.platform != "darwin":
        raise HTTPException(501, "reveal is only supported on macOS")

    base = dl.download_dir
    if row["kind"] == "folder":
        target = base / f"{row['owner']}-{row['repo']}" / (row["path"] or "").strip("/")
        select = False
    else:
        dest = row["children"][0].get("dest") if row["children"] else None
        target = base / dest if dest else base
        select = True
    if not target.exists():  # partial/moved/cleared — fall back to the downloads root
        target, select = base, False

    cmd = ["open", "-R", str(target)] if select else ["open", str(target)]
    subprocess.run(cmd, check=False)
    return Response(status_code=204)


@app.get("/downloads", response_model=DownloadsResponse)
def list_downloads(dl: DownloadManager = Depends(get_downloads)) -> DownloadsResponse:
    """Download history (persistent) with live progress folded in. Safe to poll."""
    resolved_dir = os.path.expanduser(get_settings().download_dir)
    return DownloadsResponse(
        available=dl.available,
        download_dir=resolved_dir,
        free_bytes=dl.free_bytes() if dl.available else None,
        items=[DownloadItem(**i) for i in reconcile_and_list(dl)],
    )


@app.post("/downloads/{download_id}/pause", status_code=204)
def pause_download(
    download_id: int, dl: DownloadManager = Depends(get_downloads)
) -> Response:
    for gid in child_gids(download_id):
        try:
            dl.pause(gid)
        except Exception:  # noqa: BLE001 — already-finished children are fine
            pass
    return Response(status_code=204)


@app.post("/downloads/{download_id}/resume", status_code=204)
def resume_download(
    download_id: int, dl: DownloadManager = Depends(get_downloads)
) -> Response:
    for gid in child_gids(download_id):
        try:
            dl.resume(gid)
        except Exception:  # noqa: BLE001
            pass
    return Response(status_code=204)


@app.delete("/downloads/{download_id}", status_code=204)
def cancel_download(
    download_id: int, dl: DownloadManager = Depends(get_downloads)
) -> Response:
    for gid in child_gids(download_id):
        try:
            dl.cancel(gid)
        except Exception:  # noqa: BLE001
            pass
    mark_status(download_id, "removed")
    return Response(status_code=204)
