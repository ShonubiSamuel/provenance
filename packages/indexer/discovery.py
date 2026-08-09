"""Discovery collection engine — get past GitHub's 1000-result ceiling.

GitHub code search returns at most 1000 items per query but reports the *true*
`total_count`. When a query exceeds 1000 we split it into mutually-exclusive slices that
each return < 1000, page through every slice, and union the results. This module does all
the GitHub calls + splitting but touches NO database, so it is fully unit-testable with a
fake client (see tests/test_discovery.py).

Collection strategy, in order:

1. **Harvest, then split.** Page the flat query to its 1000-result ceiling first, so the
   user has rows in hand before we spend a call on anything clever.
2. **Refuse the impossible.** Past `sample_above` reported matches, bisection cannot
   finish in any realistic budget; we keep the flat sample and say so.
3. **Recursive file-size bisection** via the `size:` qualifier for everything in
   between. Size bands are disjoint and exhaustive, so unioning them is complete with no
   dedup needed and without knowing which languages appear up front. If the deployment's
   GitHub tier rejects `size:` (HTTP 422), we keep the flat sample and flag truncation
   rather than silently dropping the tail.
4. **Stop when GitHub goes dark.** A code search that reports matches and returns none
   is a degraded backend, not an empty result set — abort and report it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import httpx

# Code search does not index files larger than this, so [0, MAX_INDEXED_SIZE] is the
# complete searchable range.
MAX_INDEXED_SIZE = 384 * 1024
PER_PAGE = 100
PAGE_CAP = 10  # 10 * 100 = the hard 1000-item ceiling
MAX_SPLIT_DEPTH = 20  # log2(384*1024) ≈ 19, enough to bisect to single-byte bands
DEFAULT_SEARCH_CALL_BUDGET = 300  # protects the scarce SEARCH rate bucket per discovery

# Above this reported total, splitting is arithmetically hopeless and pretending
# otherwise is the difference between "collecting…" forever and an honest answer. At 100
# results per call and ~8 calls/min, the call budget caps what any single discovery can
# physically retrieve; a query reporting millions gets its first 1000 and a note telling
# the user to narrow it. (`unity` reports ≈212,000,000 — that is 24 days of calls.)
DEFAULT_SAMPLE_ABOVE = 25_000

# Consecutive "total_count > 0 but items empty" responses before we stop believing
# GitHub and abort. The client already retries each call a few times, so reaching this
# means the code-search backend is genuinely refusing to serve results.
DEGRADED_TOLERANCE = 3


# Extension → Linguist-ish language, so the per-file `detected_language` facet is
# meaningful even though code search only tells us the repo's primary language.
EXT_LANGUAGE: dict[str, str] = {
    ".cs": "C#", ".shader": "ShaderLab", ".hlsl": "HLSL", ".cginc": "HLSL",
    ".glsl": "GLSL", ".compute": "HLSL", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".py": "Python", ".cpp": "C++",
    ".cc": "C++", ".h": "C", ".hpp": "C++", ".c": "C", ".java": "Java", ".go": "Go",
    ".rs": "Rust", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".asmdef": "JSON",
    ".uss": "USS", ".uxml": "UXML", ".cginc ": "HLSL",
}


@dataclass
class DiscoveredFile:
    """One matched file, DB-agnostic. Persisted by the discovery stage."""

    repo_github_id: int
    repo_full_name: str
    repo_language: str | None
    path: str
    sha: str | None
    snippet: str | None
    extension: str | None
    detected_language: str | None
    path_prefix: str


@dataclass
class DiscoveryStats:
    search_calls: int = 0
    total_reported: int = 0
    collected: int = 0
    truncated: bool = False
    truncated_bands: list[tuple[int, int, int]] = field(default_factory=list)
    size_split_used: bool = False
    hit_call_budget: bool = False
    cancelled: bool = False
    # Set when the query was too broad to collect in full and we deliberately kept only
    # the first page-set. Distinct from `truncated`, which means "we tried and lost some".
    sampled: bool = False
    # Consecutive degraded responses (a count with no items), and the flag we raise once
    # they exceed DEGRADED_TOLERANCE.
    empty_responses: int = 0
    degraded: bool = False

    def note_page(self, data: dict) -> None:
        """Track GitHub's degraded-success mode: HTTP 200, a real `total_count`, and an
        empty `items` array. Counted consecutively so one blip doesn't abort a healthy
        collection."""
        if data.get("total_count") and not (data.get("items") or []):
            self.empty_responses += 1
            if self.empty_responses >= DEGRADED_TOLERANCE:
                self.degraded = True
        else:
            self.empty_responses = 0


class _Cancelled(Exception):
    """Raised internally when the progress callback asks discovery to stop."""


class _Degraded(Exception):
    """Raised internally when GitHub keeps reporting matches it will not return."""


def _extension(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        return "." + name.rsplit(".", 1)[-1].lower()
    return None


def _top_prefix(path: str, depth: int = 2) -> str:
    parts = path.split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[: min(depth, len(parts) - 1)])


def _extract_snippet(item: dict, *, max_len: int = 2000) -> str | None:
    fragments = [
        tm.get("fragment", "")
        for tm in (item.get("text_matches") or [])
        if tm.get("fragment")
    ]
    if not fragments:
        return None
    return "\n…\n".join(fragments)[:max_len]


def _to_file(item: dict) -> DiscoveredFile:
    repo = item["repository"]
    path = item["path"]
    ext = _extension(path)
    repo_lang = repo.get("language")
    return DiscoveredFile(
        repo_github_id=repo["id"],
        repo_full_name=repo["full_name"],
        repo_language=repo_lang,
        path=path,
        sha=item.get("sha"),
        snippet=_extract_snippet(item),
        extension=ext,
        detected_language=EXT_LANGUAGE.get(ext or "", repo_lang),
        path_prefix=_top_prefix(path),
    )


class DiscoveryEngine:
    """Runs one keyword's discovery against an injected client exposing
    `async search_code(query, *, page, per_page)`.

    `progress_cb(stats) -> bool` (optional) is invoked before every GitHub call: it lets
    the caller stream live progress to the UI AND cancel a long collection (return
    False) — a saturated query at ~28 search calls/min can legitimately run for many
    minutes, and users need both a heartbeat and an exit.

    `on_files(batch)` (optional) is invoked with each page's worth of DiscoveredFiles as
    soon as they are absorbed, so the caller can persist INCREMENTALLY — results reach
    the UI within seconds instead of after the entire multi-minute collection.
    """

    def __init__(
        self,
        gh,
        *,
        call_budget: int = DEFAULT_SEARCH_CALL_BUDGET,
        sample_above: int = DEFAULT_SAMPLE_ABOVE,
        progress_cb=None,
        on_files=None,
    ) -> None:
        self._gh = gh
        self._budget = call_budget
        self._sample_above = sample_above
        self._progress_cb = progress_cb
        self._on_files = on_files

    def _absorb(self, data: dict, out: list[DiscoveredFile], stats: DiscoveryStats) -> None:
        batch = [_to_file(item) for item in data.get("items", [])]
        out.extend(batch)
        stats.collected += len(batch)
        if self._on_files is not None and batch:
            self._on_files(batch)

    async def _search(self, query: str, page: int, stats: DiscoveryStats) -> dict:
        if self._progress_cb is not None and not self._progress_cb(stats):
            stats.cancelled = True
            raise _Cancelled()
        stats.search_calls += 1
        data = await self._gh.search_code(query, page=page, per_page=PER_PAGE)
        stats.note_page(data)
        if stats.degraded:
            # Every further call would burn the same scarce quota for the same nothing.
            raise _Degraded()
        return data

    def _budget_left(self, stats: DiscoveryStats) -> bool:
        if stats.search_calls >= self._budget:
            stats.hit_call_budget = True
            stats.truncated = True
            return False
        return True

    async def _page_through(
        self, query: str, total: int, first: dict, out: list[DiscoveredFile], stats: DiscoveryStats
    ) -> None:
        """Absorb `first` (page 1) then fetch remaining pages up to the 1000 ceiling."""
        self._absorb(first, out, stats)
        pages = min(PAGE_CAP, math.ceil(min(total, 1000) / PER_PAGE))
        for page in range(2, pages + 1):
            if not self._budget_left(stats):
                return
            data = await self._search(query, page, stats)
            self._absorb(data, out, stats)

    async def _collect_range(
        self, base: str, lo: int, hi: int, out: list[DiscoveredFile], stats: DiscoveryStats, depth: int
    ) -> None:
        if not self._budget_left(stats):
            return
        query = f"{base} size:{lo}..{hi}"
        first = await self._search(query, 1, stats)
        total = first.get("total_count", 0)
        if total == 0:
            return

        if total > 1000 and hi > lo and depth < MAX_SPLIT_DEPTH and self._budget_left(stats):
            mid = (lo + hi) // 2
            await self._collect_range(base, lo, mid, out, stats, depth + 1)
            await self._collect_range(base, mid + 1, hi, out, stats, depth + 1)
            return

        if total > 1000:  # a single unsplittable band still saturates — flag, don't hide
            stats.truncated = True
            stats.truncated_bands.append((lo, hi, total))
        await self._page_through(query, total, first, out, stats)

    async def run(self, base_query: str) -> tuple[list[DiscoveredFile], DiscoveryStats]:
        out: list[DiscoveredFile] = []
        stats = DiscoveryStats()
        try:
            await self._run(base_query, out, stats)
        except (_Cancelled, _Degraded):
            pass  # partial `out` + the reason flag go back to the caller
        return out, stats

    async def _run(
        self, base_query: str, out: list[DiscoveredFile], stats: DiscoveryStats
    ) -> None:
        # Cheap path: probe flat. Most queries are under 1000 and need no splitting.
        flat_first = await self._search(base_query, 1, stats)
        total = flat_first.get("total_count", 0)
        stats.total_reported = total
        if total == 0:
            return

        # HARVEST BEFORE YOU SPLIT. The flat query can always yield its first 1000
        # results, so take them now — the user has real rows within seconds. Only then do
        # we spend calls on bisection, which is pure overhead until its first leaf lands.
        # (Previously only page 1 was absorbed here, so a big query showed 100 rows and
        # then nothing for many minutes while the bisector probed size bands.)
        await self._page_through(base_query, total, flat_first, out, stats)
        if total <= 1000:
            return

        if total > self._sample_above:
            # Splitting cannot finish in any sane budget. Say so instead of spending
            # half an hour proving it.
            stats.sampled = True
            stats.truncated = True
            return

        # Split by size band to get past the 1000 ceiling. The root band is the whole
        # searchable range and would just re-report `total`, so bisect straight into
        # halves rather than paying for that probe. Banded collection re-yields the flat
        # page-set; persistence is idempotent (upsert + get-before-add), so it converges.
        mid = MAX_INDEXED_SIZE // 2
        try:
            stats.size_split_used = True
            await self._collect_range(base_query, 0, mid, out, stats, 1)
            await self._collect_range(base_query, mid + 1, MAX_INDEXED_SIZE, out, stats, 1)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 422:
                # This deployment's tier rejects `size:` — the flat 1000 we already have
                # is all we can get, and the tail is genuinely lost.
                stats.size_split_used = False
                stats.truncated = True
            else:
                raise
