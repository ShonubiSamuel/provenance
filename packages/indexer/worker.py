"""Background worker: drains the durable queue, dispatching jobs to their stage.

Runs as a set of concurrent coroutines sharing one GitHubClient (hence one RateLimiter),
so the rate buckets are enforced process-wide no matter how many workers run. Started
from the FastAPI lifespan; can also be run standalone for headless indexing.
"""
from __future__ import annotations

import asyncio

from packages.core.enums import JobStatus, JobType
from packages.core.settings import get_settings
from packages.github.client import GitHubClient
from packages.github.ratelimit import RateLimiter
from packages.indexer.queue import (
    claim_next,
    complete,
    mark_search_error,
    reconcile_search_readiness,
)
from packages.indexer.stages import STAGE_DISPATCH

# After this many attempts a job goes terminal ERROR instead of requeueing — the design
# counts 'error' as complete for search readiness, so one poisoned job can't hold a
# search in 'enriching' forever.
MAX_ATTEMPTS = 5


def build_client() -> GitHubClient:
    s = get_settings()
    limiter = RateLimiter(
        search_rpm=s.rate_search_rpm, core_rpm=s.rate_core_rpm, graphql_rpm=s.rate_graphql_rpm
    )
    return GitHubClient(settings=s, limiter=limiter)


async def _worker_loop(gh: GitHubClient, stop: asyncio.Event, idle_sleep: float) -> None:
    while not stop.is_set():
        job = claim_next()
        if job is None:
            await asyncio.sleep(idle_sleep)
            continue
        handler = STAGE_DISPATCH.get(job["type"])
        if handler is None:
            complete(job["id"], JobStatus.ERROR, error=f"unknown job type {job['type']}")
            continue
        try:
            await handler(gh, job["payload"])
            complete(job["id"], JobStatus.DONE)
        except Exception as exc:  # noqa: BLE001 — queue must survive any stage failure
            if job["attempts"] >= MAX_ATTEMPTS:
                complete(job["id"], JobStatus.ERROR, error=repr(exc))  # terminal
                # A dead discovery job means its search can never complete.
                if job["type"] == str(JobType.DISCOVERY):
                    mark_search_error(job["payload"]["search_id"])
            else:
                backoff = min(300, 2 ** job["attempts"])
                complete(job["id"], JobStatus.ERROR, error=repr(exc),
                         retry_after_seconds=backoff)
        reconcile_search_readiness()


class WorkerPool:
    def __init__(
        self,
        concurrency: int = 6,
        idle_sleep: float = 1.0,
        client: GitHubClient | None = None,
    ) -> None:
        self.concurrency = concurrency
        self.idle_sleep = idle_sleep
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # An injected client is shared with the read path (API inspection endpoints) so
        # a single RateLimiter governs ALL GitHub traffic. When shared, the pool does not
        # own it and must not close it — its owner (the API lifespan) does.
        self._gh: GitHubClient | None = client
        self._owns_client = client is None

    async def start(self) -> None:
        if self._gh is None:
            self._gh = build_client()
            self._owns_client = True
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(_worker_loop(self._gh, self._stop, self.idle_sleep))
            for _ in range(self.concurrency)
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._gh is not None and self._owns_client:
            await self._gh.aclose()
