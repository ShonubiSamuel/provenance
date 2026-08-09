"""GitHub client failure handling — the cases that previously turned into a spinner.

Every test here corresponds to something GitHub was observed doing to a real search:
a 408 timeout, a 200 that reports matches and returns none, and a 401. All three used to
end as either "0 results" or a retry storm with no explanation.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from packages.core.settings import Settings
from packages.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubUnavailable,
)
from packages.github.ratelimit import RateLimiter


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Backoff is real seconds in production and pointless in a test."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _client(handler) -> GitHubClient:
    settings = Settings(gh_pat="test-token", database_url="sqlite:///:memory:")
    gh = GitHubClient(
        settings=settings,
        limiter=RateLimiter(search_rpm=600, core_rpm=600, graphql_rpm=600),
    )
    gh._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return gh


def _search_headers(**extra: str) -> dict[str, str]:
    return {"x-ratelimit-resource": "code_search", **extra}


@pytest.mark.asyncio
async def test_408_is_retried_then_reported_in_plain_words():
    """GitHub's 'Request timed out while fetching results' is transient — retry it —
    but if it never clears, say so instead of raising a bare HTTP error."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            408,
            json={"message": "Request timed out while fetching results."},
            headers=_search_headers(),
        )

    gh = _client(handler)
    with pytest.raises(GitHubUnavailable) as exc:
        await gh.search_code("MeshCombiner")

    assert calls["n"] == 5  # the initial attempt plus four retries
    assert "timing out" in exc.value.user_message
    await gh.aclose()


@pytest.mark.asyncio
async def test_408_that_clears_on_retry_succeeds():
    responses = [
        httpx.Response(408, json={"message": "timed out"}, headers=_search_headers()),
        httpx.Response(
            200,
            json={"total_count": 1, "items": [{"repository": {}, "path": "a.cs"}]},
            headers=_search_headers(),
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    gh = _client(handler)
    data = await gh.search_code("MeshCombiner")
    assert len(data["items"]) == 1
    await gh.aclose()


@pytest.mark.asyncio
async def test_count_without_items_is_retried_and_flagged_degraded():
    """The silent killer: HTTP 200, `total_count: 1304`, `items: []`. Indistinguishable
    from an empty result set unless you compare the two — which is exactly why a search
    could run for an hour and store nothing."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"total_count": 1304, "items": []}, headers=_search_headers()
        )

    gh = _client(handler)
    data = await gh.search_code("MeshCombiner")

    # Re-asked once before believing it — cheap against a 10/min quota, while discovery
    # still needs several consecutive degraded calls before it aborts a collection.
    assert calls["n"] == 2
    assert data["total_count"] == 1304 and data["items"] == []
    assert gh.health.degraded_since is not None
    assert "degraded" in (gh.health.last_error or "")
    await gh.aclose()


@pytest.mark.asyncio
async def test_a_genuinely_empty_result_is_not_degraded():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"total_count": 0, "items": []}, headers=_search_headers()
        )

    gh = _client(handler)
    await gh.search_code("nothing-matches-this")
    assert gh.health.degraded_since is None
    await gh.aclose()


@pytest.mark.asyncio
async def test_401_fails_immediately_with_an_actionable_message():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"message": "Bad credentials"})

    gh = _client(handler)
    with pytest.raises(GitHubAuthError) as exc:
        await gh.search_code("anything")

    assert calls["n"] == 1  # never retried: wrong credentials stay wrong
    assert "GH_PAT" in exc.value.user_message
    assert ".env" in exc.value.user_message
    await gh.aclose()


@pytest.mark.asyncio
async def test_rate_limit_headers_reach_the_status_endpoint():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"total_count": 0, "items": []},
            headers=_search_headers(**{
                "x-ratelimit-limit": "10",
                "x-ratelimit-remaining": "3",
                "x-ratelimit-reset": "1786292392",
            }),
        )

    gh = _client(handler)
    await gh.search_code("anything")
    assert gh.health.search_limit == 10
    assert gh.health.search_remaining == 3
    assert gh.health.search_reset_epoch == 1786292392
    await gh.aclose()
