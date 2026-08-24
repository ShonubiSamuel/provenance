# Provenance — Data Model & Pipeline Contract

Status: **implemented** — stages 1–3 and the detector framework are built and tested
(see README.md for current status); clone-precise history and FTS5-backed search are
still to come. This document defines the two things that are expensive to change later:
the **storage schema** and the **pipeline stage contracts**.

---

## 0. Design principles baked into the schema

1. **Repos are deduplicated globally; matches are per-search.** A repo enriched once
   serves every search that touches it. We never re-fetch repo metadata per keyword.
2. **Asset history is keyed by `(repo, path)`, not by search.** The "first appeared"
   fact about `Assets/HighlightPlus` in repo X is universal — computed once, reused.
3. **Everything is idempotent + checkpointed.** Every row that represents work has a
   deterministic natural key and a status, so re-running a job is a no-op or a resume.
4. **Truncation is first-class.** If a search hit GitHub's 1000-result ceiling, we
   record it, because it biases facets and researchers must know.
5. **Repository interface over raw SQL.** All access goes through repository classes so
   the SQLite→Postgres swap is an adapter change, not a rewrite.

---

## 1. Schema (SQLite, WAL) — logical model

Types shown are SQLite-friendly; SQLAlchemy models map 1:1. `ts` = ISO-8601 UTC text.

### `repositories` — the global, deduplicated repo cache
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | local surrogate |
| `github_id` | INTEGER UNIQUE | stable GitHub node id |
| `full_name` | TEXT UNIQUE | `owner/name` |
| `owner` | TEXT | |
| `name` | TEXT | |
| `owner_type` | TEXT | `User` / `Organization` |
| `description` | TEXT | |
| `stars` | INTEGER | |
| `forks` | INTEGER | |
| `primary_language` | TEXT | GitHub's `language` |
| `license_spdx` | TEXT | e.g. `MIT`, null if none |
| `is_fork` | INTEGER(bool) | |
| `is_archived` | INTEGER(bool) | |
| `parent_full_name` | TEXT | set when `is_fork`, for fork-collapse |
| `default_branch` | TEXT | |
| `created_at` | ts | repo created |
| `pushed_at` | ts | last push — the freshness gate |
| `updated_at` | ts | repo metadata updated |
| `unity_version` | TEXT | detector output, nullable |
| `etag` | TEXT | for conditional refresh (304) |
| `pushed_at_at_last_index` | ts | skip enrichment if unchanged |
| `last_enriched_at` | ts | |

### `repo_languages` — many-to-many (GitHub languages breakdown)
| column | type | notes |
|---|---|---|
| `repo_id` | FK → repositories | |
| `language` | TEXT | |
| `bytes` | INTEGER | for weighting facets |
| PK | (`repo_id`,`language`) | |

### `matches` — a matched file in a repo (universal, not per-search)
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `repo_id` | FK → repositories | |
| `path` | TEXT | full path in default branch |
| `filename` | TEXT | basename |
| `extension` | TEXT | `.cs`, `.shader`, … |
| `detected_language` | TEXT | Linguist-style, may differ from repo primary |
| `size_bytes` | INTEGER | |
| `content_sha` | TEXT | git blob sha, for dedup + change detection |
| `snippet` | TEXT | cached matched-lines context (nullable) |
| `path_prefix` | TEXT | precomputed top dir(s) for the "Common Paths" facet |
| UNIQUE | (`repo_id`,`path`) | |

### `searches` — a keyword query and its lifecycle
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `keyword` | TEXT | raw user input |
| `normalized_query` | TEXT | how we actually queried GitHub |
| `search_type` | TEXT | resolved type — `keyword`/`phrase`/`path`/`filename`/`ext`/`language`/`raw` (the UI sends `auto`; see §1.1) |
| `status` | TEXT | `pending`/`discovering`/`enriching`/`ready`/`error` |
| `truncated` | INTEGER(bool) | hit the 1000-result ceiling somewhere |
| `sampled` | INTEGER(bool) | too broad to collect in full; we kept the first 1000 |
| `total_matches` | INTEGER | how many matches we actually hold |
| `reported_matches` | INTEGER | how many GitHub claimed existed |
| `note` | TEXT | plain-language reason the outcome is imperfect; shown verbatim in the UI |

`total_matches` and `reported_matches` are deliberately separate. "0 collected of
≈1,304 reported" is a failure; "1,304 of 1,304" is a success; collapsing them into one
number makes the two indistinguishable, which is exactly how a broken collection used to
render as a spinner.

### 1.1 Query auto-detection

The UI has ONE input. `packages/core/query.py` classifies whatever is typed — a GitHub
URL (repo, blob, tree, raw), a path, a filename, a bare extension, a quoted phrase,
existing GitHub qualifier syntax, or a plain keyword — and returns the query to run plus
a one-line explanation. `/detect` exposes the same function so the UI can show its guess
before you commit, and `/index` accepts `search_type: auto` (the default). A concrete
`search_type` is a user override and is honoured literally.
| `created_at` | ts | |
| `completed_at` | ts | |
| `refreshed_at` | ts | last re-index |

### `search_results` — which matches belong to which search
| column | type | notes |
|---|---|---|
| `search_id` | FK → searches | |
| `match_id` | FK → matches | |
| `rank` | INTEGER | GitHub relevance order as returned |
| PK | (`search_id`,`match_id`) | |

### `asset_history` — the headline feature, keyed by (repo, path)
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `repo_id` | FK → repositories | |
| `path` | TEXT | the asset/path analyzed |
| `first_appeared_at` | ts | **the sortable "asset added" date** |
| `first_commit_sha` | TEXT | commit that introduced it |
| `last_modified_at` | ts | most recent commit touching it |
| `method` | TEXT | `api-approx` (commits-for-path) / `clone-precise` (--follow) |
| `follows_renames` | INTEGER(bool) | true only for clone-precise |
| `computed_at` | ts | |
| UNIQUE | (`repo_id`,`path`,`method`) | both tiers can coexist |

### `search_jobs` — readiness tracking
| column | type | notes |
|---|---|---|
| `search_id` | FK → searches | |
| `job_id` | FK → jobs | |
| PK | (`search_id`,`job_id`) | |

Jobs are global and deduplicated; this table records which jobs a given search's
discovery emitted (including dedup-hits on jobs another search created), so readiness
can be computed per search.

### `jobs` — durable, resumable queue
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `type` | TEXT | `discovery`/`repo_enrichment`/`asset_history`/`detector` |
| `dedup_key` | TEXT UNIQUE | natural key → avoids duplicate work |
| `payload` | TEXT(json) | inputs |
| `status` | TEXT | `queued`/`running`/`done`/`error`/`skipped` |
| `attempts` | INTEGER | |
| `not_before` | ts | backoff / rate-limit deferral |
| `last_error` | TEXT | |
| `created_at` / `updated_at` | ts | |

### `fts_matches` — FTS5 virtual table
Full-text over `matches.snippet`, `matches.path`, `matches.filename`. Enables offline
phrase/prefix search; regex runs app-side over the candidate set FTS narrows. Kept in
sync via triggers on `matches`.

**Facets are computed on read** (GROUP BY over `search_results` joined to
`repositories`/`matches`/`asset_history`), with a per-search cache table added later if
profiling demands it. No premature materialization.

---

## 2. Pipeline stage contracts

Each stage: **input → GitHub/git calls → rows written → jobs emitted**. `dedup_key`
guarantees idempotency.

### Stage 1 — `DiscoveryJob`  *(implemented — packages/indexer/discovery.py)*
- **Input:** `search_id`, `normalized_query`.
- **Does:** in strict order —
  1. **Harvest first.** Pages the flat query to its 1000-item ceiling and persists each
     page as it lands, so results are on screen before any cleverness begins.
  2. **Refuse the impossible.** Above `discovery_sample_above` reported matches (default
     25,000) bisection cannot finish in any realistic budget — sets `searches.sampled`
     with a note telling the user to narrow the query, instead of spending the whole
     call budget proving it. (`unity` reports ≈212,000,000: ~24 days of calls.)
  3. **Bisect by file size** (`size:lo..hi`) for everything in between — disjoint,
     exhaustive bands that union with no dedup — paging each sub-slice. A band that
     still saturates, or a hit on the per-discovery call budget, sets
     `searches.truncated` and is recorded in `stats.truncated_bands` (no silent drops).
     If the GitHub tier rejects `size:` (HTTP 422), the flat harvest stands and
     truncation is flagged.
  4. **Stop when GitHub goes dark.** A response carrying a `total_count` and an empty
     `items` array is a degraded backend, not an empty result set. After
     `DEGRADED_TOLERANCE` of them the collection aborts and the search lands in `error`
     with an explanation. A search that reports **zero** everywhere is checked against a
     control query (`GitHubClient.CANARY_QUERY`) before "no matches" is believed.

  Snippets come from the **text-match media type** (`text_matches[].fragment`) — no
  separate content fetch; blob `sha`, extension, and per-file language captured inline.
- **Writes:** upserts `repositories` (shallow: full_name+github_id only), upserts
  `matches` (with snippet/sha/extension/language/prefix, batched), inserts `search_results`.
- **Emits:** one `repo_enrichment` job per distinct repo; one `asset_history` job per
  distinct `(repo, path_prefix)` (directory-prefix granularity by default — see §3).
- **dedup_key:** `discovery:{search_id}`.
- **Rate bucket:** `search` — GitHub's `code_search` resource allows **10 req/min**, and
  punishes *clumping* harder than volume: the bucket is therefore capped at `max_burst=1`
  so calls are paced evenly rather than fired in a burst that trips secondary limits.

### Stage 2 — `RepoEnrichmentJob`
- **Input:** `repo_id`.
- **Does:** one **GraphQL** query pulling stars/forks/created/pushed/updated/license/
  languages/default-branch/fork+archive/parent. Uses stored `etag`; a `pushed_at`
  unchanged since `pushed_at_at_last_index` → **skip** (status `skipped`).
- **Writes:** fills `repositories`, replaces `repo_languages`.
- **Emits:** `detector` jobs (unity_version, vendored-vs-package).
- **dedup_key:** `enrich:{repo_id}`.
- **Rate bucket:** `graphql`.

### Stage 3 — `AssetHistoryJob` (api-approx tier)  *(implemented — client.commit_bounds_for_path)*
- **Input:** `repo_id`, `path` (a **directory prefix** by default, e.g.
  `Assets/HighlightPlus`; file-level path only when the user requests it on demand).
- **Does:** `GET /repos/{o}/{r}/commits?path={path}&per_page=1`, read `Link rel="last"`,
  fetch last page → oldest commit = `first_appeared_at`; page 1 → `last_modified_at`.
  ~2 calls.
- **Writes:** `asset_history` row with `method='api-approx'`.
- **dedup_key:** `history:approx:{repo_id}:{path}`.
- **Rate bucket:** `core`.
- **Precise tier** (`clone-precise`, blobless clone + `git log --follow`) is a separate
  on-demand job, never auto-enqueued.

### Stage 4 — `DetectorJob` (pluggable)  *(framework + unity_version implemented)*
- **Input:** `repo_id`, `detector_name`.
- **Does:** dispatches to the detector registry (`packages/detectors`). A detector fetches
  the one file it needs (e.g. `ProjectSettings/ProjectVersion.txt`) and returns fields the
  stage persists onto the repo. `unity_version` is the first real detector; new ones
  register via `base.register(...)` without touching stages 1–3.
- **dedup_key:** `detect:{detector_name}:{repo_id}`.
- **Rate bucket:** `core`.

### Search readiness  *(implemented — queue.reconcile_search_readiness)*
`searches.status='ready'` once discovery is done AND all emitted enrichment/history jobs
are `done`/`skipped`/`error`. Discovery links every job it emits via `search_jobs`;
workers call `reconcile_search_readiness()` after each job completion, and discovery
calls it once at the end (covers zero-result searches and all-dedup-hit cases). Jobs
that keep failing go terminal `error` after `MAX_ATTEMPTS` so a poisoned job cannot
hold a search in `enriching`. Detector jobs (emitted by enrichment) do NOT gate
readiness. The UI streams partial results before then, with a progress indicator and
per-row freshness/`method` badges.

---

## 3. Resolved decisions (2026-07-11)

1. **Snippet caching → YES.** `matches.snippet` stores matched-line context. This powers
   offline regex/phrase search and the rich result view — a core differentiator.
2. **Asset-history granularity → directory prefix by default**, file-level on demand.
   Stage 1 emits one `asset_history` job per distinct `(repo, path_prefix)`.
3. **Fork handling → collapse to parent by default**, with a UI toggle to expand.
   Uses `repositories.parent_full_name`; fork rows are grouped under their origin.
