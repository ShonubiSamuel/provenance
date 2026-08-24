# Provenance

**Search GitHub code from a local, historical index — and sort results by when the
matching file actually first appeared, which GitHub's own code search can't do.**

A local research index over GitHub code. Type **anything** into one box — a name
(`HighlightPlus`), a path (`Assets/HighlightPlus`), a filename (`Highlight.cs`), an
extension (`*.shader`), a quoted phrase, or a GitHub link — and it works out what you
meant. You get filters, facets, and sorting that GitHub Code Search can't provide — most
notably **sorting repositories by when the matching asset first appeared**, reconstructed
from git history.

> Not a wrapper around GitHub search. GitHub is the *ingestion source*; all user queries
> run against a local, enriched, historical index and return instantly.

## Architecture at a glance

```
DISCOVERY (GitHub, rate-limited)  →  ENRICHMENT (GitHub + git history)  →  LOCAL INDEX (SQLite+FTS5)  →  SEARCH/FACETS (local, instant)
        write path — background jobs                                          read path — the UI
```

- **Backend:** Python + FastAPI + httpx + SQLAlchemy, DB-backed durable job queue.
- **Auth:** GitHub App (single token-bucket limiter, separate `core`/`search`/`graphql` buckets).
- **Storage:** SQLite (WAL) + FTS5 behind a repository interface → Postgres/pgvector later.
- **Frontend:** React + Vite + TS (localhost web app first; Tauri wrap deferred).

Full design: [docs/DATA_MODEL_AND_PIPELINE.md](docs/DATA_MODEL_AND_PIPELINE.md).

## Layout

```
apps/api/          FastAPI app (read: search/facets; inspect: repo browse/preview/download)
apps/web/          React + Vite + TS + Tailwind frontend (facet sidebar, sortable results)
packages/core/     domain models + settings
packages/storage/  SQLAlchemy ORM, FTS5, repository interfaces + SQLite adapter
packages/github/   httpx client, GitHub App auth, multi-bucket rate limiter
packages/indexer/  durable job queue + the four pipeline stages
packages/detectors/ pluggable enrichers (Unity version, vendored-vs-package, ...)
docs/              design docs
```

## Quickstart (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in GitHub App creds (or GH_PAT for local dev)
uvicorn apps.api.main:app --reload --port 8787
```

Frontend (proxies `/api/*` to the backend — run both):

```bash
cd apps/web
npm install
npm run dev               # http://localhost:5173
```

Downloads are handled by a backend-managed **aria2** engine (`brew install aria2`),
not the browser: files/folders/repos queue into `DOWNLOAD_DIR` (default
`~/Data/datasets/unity-repo-corpus`, configurable in `.env`) with parallel
connections, pause/resume, and a persistent history.

Status: **functional** — index a query, watch results stream in as enrichment
completes, filter via facets, sort by asset-added, inspect any repo in-app, and
download files/folders/repos through the built-in download manager.
Clone-precise history tier and FTS5 offline search still to come.

## When a search doesn't work

Every failure mode has a stated reason rather than an endless spinner. `GET /status`
reports credentials, token expiry, the live `code_search` quota, and whether GitHub is
degraded; the UI renders it as a banner. Per search, `note` explains an imperfect
outcome in plain words. The three that matter:

- **"GitHub reported ≈N matches but returned none."** GitHub's code search answers an
  overloaded query with HTTP 200, a real count, and an empty `items` array (and
  sometimes a 408 or 503). Nothing local is wrong; retry in a few minutes.
- **"…including a control query that always matches."** A search that found nothing is
  re-checked against a known-good query before "no matches" is believed, so an outage is
  never reported as an empty result.
- **"…far too many to index."** GitHub serves at most 1,000 results per query at
  10 calls/minute. A query reporting millions (`unity` reports ≈212,000,000) gets its
  first 1,000 and a nudge to narrow it — see `DISCOVERY_SAMPLE_ABOVE`.
