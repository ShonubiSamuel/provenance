"""Discovery engine tests — no network. A FakeGH models GitHub code search, including
the 1000-item ceiling and the `size:` split qualifier, so we can prove completeness.
"""
from __future__ import annotations

import httpx
import pytest

from packages.indexer.discovery import (
    MAX_INDEXED_SIZE,
    DiscoveryEngine,
    _extract_snippet,
)


def _item(repo_id: int, full_name: str, path: str, *, sha: str = "abc", fragments=None):
    it = {
        "repository": {"id": repo_id, "full_name": full_name, "language": "C#"},
        "path": path,
        "sha": sha,
    }
    if fragments is not None:
        it["text_matches"] = [{"fragment": f} for f in fragments]
    return it


class FakeGH:
    """Serves a fixed universe of files, honouring page/per_page and an optional
    `size:lo..hi` qualifier so size-band splitting can be exercised deterministically.
    Caps items at 1000 like GitHub, while reporting the true total_count.
    """

    def __init__(self, universe: list[dict], *, size_of=None, support_size=True):
        self.universe = universe
        self.size_of = size_of or (lambda item: 100)  # default: all size 100
        self.support_size = support_size
        self.calls: list[str] = []

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100) -> dict:
        self.calls.append(query)
        lo = hi = None
        if "size:" in query:
            if not self.support_size:
                raise httpx.HTTPStatusError(
                    "unprocessable", request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(422),
                )
            band = query.split("size:", 1)[1].strip()
            lo_s, _, hi_s = band.partition("..")
            lo, hi = int(lo_s), int(hi_s)

        matched = [
            it for it in self.universe
            if (lo is None or lo <= self.size_of(it) <= hi)
        ]
        total = len(matched)
        capped = matched[:1000]
        start = (page - 1) * per_page
        return {"total_count": total, "items": capped[start : start + per_page]}


async def _run(gh: FakeGH):
    return await DiscoveryEngine(gh).run("HighlightPlus")


def test_snippet_from_text_matches():
    it = _item(1, "a/b", "x.cs", fragments=["line one", "line two"])
    assert _extract_snippet(it) == "line one\n…\nline two"
    assert _extract_snippet(_item(1, "a/b", "x.cs")) is None


@pytest.mark.asyncio
async def test_small_result_set_no_split():
    universe = [_item(i, f"o/r{i}", f"Assets/HP/f{i}.cs") for i in range(250)]
    gh = FakeGH(universe)
    files, stats = await _run(gh)
    assert len(files) == 250
    assert stats.truncated is False
    assert stats.size_split_used is False
    assert not any("size:" in c for c in gh.calls)  # never needed to split


@pytest.mark.asyncio
async def test_split_recovers_more_than_1000():
    # 2500 files spread across distinct sizes so size-band bisection separates them.
    universe = [_item(i, f"o/r{i}", f"Assets/HP/f{i}.cs") for i in range(2500)]
    sizes = {id(it): (i * 150) % MAX_INDEXED_SIZE for i, it in enumerate(universe)}
    gh = FakeGH(universe, size_of=lambda it: sizes[id(it)])
    files, stats = await _run(gh)
    # Completeness: every distinct path recovered exactly once.
    paths = {f.path for f in files}
    assert len(paths) == 2500
    assert stats.size_split_used is True
    assert stats.total_reported == 2500


@pytest.mark.asyncio
async def test_unsplittable_band_flags_truncation():
    # 1500 files ALL the same size → no size band can get under 1000 → truncated.
    universe = [_item(i, f"o/r{i}", f"f{i}.cs") for i in range(1500)]
    gh = FakeGH(universe, size_of=lambda it: 500)
    files, stats = await _run(gh)
    assert stats.truncated is True
    assert stats.truncated_bands  # recorded, not silently dropped
    # The flat harvest takes the full ceiling first, then the unsplittable leaf band
    # re-yields the same 1000. Raw duplicates are expected; the persistence layer
    # converges them (upsert + get-before-add).
    assert len(files) == 2000
    assert len({(f.repo_full_name, f.path) for f in files}) == 1000  # unique matches


@pytest.mark.asyncio
async def test_422_size_qualifier_falls_back_to_flat():
    universe = [_item(i, f"o/r{i}", f"f{i}.cs") for i in range(1500)]
    gh = FakeGH(universe, support_size=False)
    files, stats = await _run(gh)
    assert stats.size_split_used is False
    assert stats.truncated is True  # honest about the dropped tail
    # The flat harvest already ran before we tried to split, so a 422 costs us nothing
    # we could have had: the 1000-item ceiling is in hand and the tail is genuinely lost.
    assert len(files) == 1000
    assert len({(f.repo_full_name, f.path) for f in files}) == 1000


@pytest.mark.asyncio
async def test_harvests_the_flat_ceiling_before_splitting():
    """Results must reach the user before we spend a single call on bisection — the
    first page-set is the difference between 'rows on screen in seconds' and 'a spinner
    for ten minutes'."""
    universe = [_item(i, f"o/r{i}", f"f{i}.cs") for i in range(2500)]
    sizes = {id(it): (i * 150) % MAX_INDEXED_SIZE for i, it in enumerate(universe)}
    gh = FakeGH(universe, size_of=lambda it: sizes[id(it)])

    seen: list[int] = []
    engine = DiscoveryEngine(gh, on_files=lambda batch: seen.append(len(batch)))
    await engine.run("HighlightPlus")

    # 10 flat pages of 100 land before the first `size:`-qualified call is made.
    first_split = next(i for i, c in enumerate(gh.calls) if "size:" in c)
    assert first_split == 10
    assert sum(seen[:10]) == 1000


@pytest.mark.asyncio
async def test_absurdly_broad_query_is_sampled_not_ground_through():
    """A query reporting millions cannot be collected at 100 results/call. Take the
    ceiling, flag it, and stop — don't burn the whole budget proving the obvious."""
    universe = [_item(i, f"o/r{i}", f"f{i}.cs") for i in range(1200)]
    gh = FakeGH(universe, size_of=lambda it: 500)
    files, stats = await DiscoveryEngine(gh, sample_above=1000).run("unity")

    assert stats.sampled is True
    assert stats.truncated is True
    assert stats.size_split_used is False
    assert not any("size:" in c for c in gh.calls)  # never even started bisecting
    assert len(files) == 1000
    assert len(gh.calls) == 10  # exactly the flat ceiling, nothing wasted


class DegradedGH:
    """GitHub's observed failure mode: HTTP 200, a truthful `total_count`, and an empty
    `items` array. Looks exactly like 'no matches' unless you check the count."""

    def __init__(self) -> None:
        self.calls = 0

    async def search_code(self, query: str, *, page: int = 1, per_page: int = 100) -> dict:
        self.calls += 1
        return {"total_count": 1304, "items": []}


@pytest.mark.asyncio
async def test_degraded_responses_abort_instead_of_looping():
    gh = DegradedGH()
    files, stats = await DiscoveryEngine(gh).run("MeshCombiner")

    assert stats.degraded is True
    assert files == []
    assert stats.total_reported == 1304  # we know what GitHub claimed…
    assert stats.collected == 0          # …and that it gave us none of it
    # Stops after the tolerance, rather than spending 300 calls on the same nothing.
    assert gh.calls == 3
