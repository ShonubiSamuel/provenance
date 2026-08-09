"""Multi-bucket async token-bucket rate limiter.

GitHub enforces *different* ceilings on different endpoint classes. Code search is the
tight one (10/min authenticated) and is the throughput ceiling of the whole system; core
and graphql are far higher. We model each as its own bucket so a burst of cheap
enrichment calls never starves — or gets starved by — the scarce search calls.

**Burst is capped separately from rate.** A classic token bucket starts full, so an
8/min search bucket happily fires all 8 calls in two seconds and then idles. GitHub reads
that as abuse: it answers with secondary-limit 403s, and — observed in the wild — with
degraded 200s carrying a `total_count` and an empty `items` array, or outright 408s. For
the search bucket we therefore set `max_burst=1`, which paces calls evenly (one every
`60/rpm` seconds) instead of clumping them.

The limiter also honours server feedback: `note_server_reset()` parks a bucket until
GitHub's `x-ratelimit-reset` / `Retry-After` when we get throttled anyway, and
`snapshot()` exposes that wait to the UI so a stalled search can say *why* it is waiting.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from packages.core.enums import RateBucket


@dataclass
class BucketState:
    """A bucket's live state, for /status. `wait_seconds` is how long the next call
    would block — 0 when a slot is free right now."""

    rate_per_minute: int
    tokens: float
    parked_seconds: float
    wait_seconds: float


class _TokenBucket:
    def __init__(
        self, rate_per_minute: int, *, max_burst: int | None = None, monotonic=time.monotonic
    ) -> None:
        self.rate_per_minute = max(1, rate_per_minute)
        # Capacity governs BURST; refill governs sustained rate. Keeping them separate is
        # what stops an idle bucket from dumping a minute's quota in one second.
        self.capacity = max(1, min(max_burst or self.rate_per_minute, self.rate_per_minute))
        self.tokens = float(self.capacity)
        self.refill_per_sec = self.rate_per_minute / 60.0
        self._monotonic = monotonic
        self._updated = monotonic()
        self._parked_until = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = now - self._updated
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self._updated = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._monotonic()
                if now < self._parked_until:
                    await asyncio.sleep(self._parked_until - now)
                    continue
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                deficit = 1.0 - self.tokens
                await asyncio.sleep(deficit / self.refill_per_sec)

    def park_until(self, epoch_seconds: float) -> None:
        """Park this bucket until a wall-clock reset reported by the server."""
        delay = epoch_seconds - time.time()
        if delay > 0:
            self._parked_until = self._monotonic() + delay

    def state(self) -> BucketState:
        now = self._monotonic()
        # Read-only projection of _refill: never mutate from a status call.
        tokens = min(self.capacity, self.tokens + (now - self._updated) * self.refill_per_sec)
        parked = max(0.0, self._parked_until - now)
        wait = parked if parked > 0 else max(0.0, (1.0 - tokens) / self.refill_per_sec)
        return BucketState(
            rate_per_minute=self.rate_per_minute,
            tokens=round(tokens, 2),
            parked_seconds=round(parked, 1),
            wait_seconds=round(wait, 1),
        )


class RateLimiter:
    """Holds one bucket per RateBucket. Shared across all workers in the process."""

    def __init__(self, *, search_rpm: int, core_rpm: int, graphql_rpm: int) -> None:
        self._buckets = {
            # No burst on search: GitHub's code-search limiter punishes clumping harder
            # than it punishes volume.
            RateBucket.SEARCH: _TokenBucket(search_rpm, max_burst=1),
            RateBucket.CORE: _TokenBucket(core_rpm, max_burst=max(1, core_rpm // 4)),
            RateBucket.GRAPHQL: _TokenBucket(graphql_rpm, max_burst=max(1, graphql_rpm // 4)),
        }

    async def acquire(self, bucket: RateBucket) -> None:
        await self._buckets[bucket].acquire()

    def note_server_reset(self, bucket: RateBucket, reset_epoch: float) -> None:
        self._buckets[bucket].park_until(reset_epoch)

    def snapshot(self) -> dict[str, BucketState]:
        return {str(name): bucket.state() for name, bucket in self._buckets.items()}
