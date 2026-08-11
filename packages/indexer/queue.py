"""DB-backed durable job queue.

Chosen over an in-memory queue so indexing survives restarts ('resume interrupted
indexing') and a `dedup_key` UNIQUE constraint gives us 'avoid duplicate work' for free:
enqueuing the same logical job twice is a no-op. `not_before` handles rate-limit deferral
and retry backoff. Claiming uses a short transaction so multiple worker coroutines don't
grab the same row.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, select

from packages.core.enums import JobStatus, JobType, SearchStatus
from packages.storage.db import session_scope
from packages.storage.orm import Job, Search, SearchJob


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(job_type: JobType, dedup_key: str, payload: dict) -> int:
    """Insert a job unless its dedup_key already exists. Returns the job id either way,
    so callers can link the job (new or pre-existing) to a search for readiness tracking.
    """
    with session_scope() as s:
        existing = s.scalar(select(Job).where(Job.dedup_key == dedup_key))
        if existing is not None:
            return existing.id
        job = Job(
            type=str(job_type),
            dedup_key=dedup_key,
            payload=json.dumps(payload),
            status=str(JobStatus.QUEUED),
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(job)
        s.flush()
        return job.id


def link_search_job(search_id: int, job_id: int) -> None:
    """Record that a search depends on a job. Idempotent (composite PK)."""
    with session_scope() as s:
        if s.get(SearchJob, {"search_id": search_id, "job_id": job_id}) is None:
            s.add(SearchJob(search_id=search_id, job_id=job_id))


def recover_orphaned_jobs() -> int:
    """Requeue jobs stuck in 'running' from a previous process. Called once at startup:
    this is a single-process app, so any RUNNING job at boot was orphaned by a crash or
    restart mid-run and would otherwise hang (its search stuck 'discovering') forever.
    Returns how many were requeued.
    """
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.status == str(JobStatus.RUNNING))).all()
        for job in rows:
            job.status = str(JobStatus.QUEUED)
            # An orphaned run is OUR fault (restart), not a failure — don't let piled-up
            # restart attempts push the job toward its terminal-error cap.
            job.attempts = 0
            job.not_before = None
            job.updated_at = _now()
        return len(rows)


def repair_search_statuses() -> int:
    """Startup repair: searches still 'pending'/'discovering' whose discovery job died
    terminally while the app was down go to 'error' (the live path does this in the
    worker, but not if the process was killed first). Returns how many were repaired.
    """
    with session_scope() as s:
        stuck = s.scalars(
            select(Search).where(Search.status.in_(["pending", "discovering"]))
        ).all()
        repaired = 0
        for search in stuck:
            job = s.scalar(select(Job).where(Job.dedup_key == f"discovery:{search.id}"))
            if job is not None and job.status == str(JobStatus.ERROR):
                search.status = str(SearchStatus.ERROR)
                search.completed_at = _now()
                repaired += 1
        return repaired


def mark_search_error(search_id: int) -> None:
    """Terminal-failure path: a discovery job that exhausted its retries flips its
    search to 'error' so the UI never polls a search that can no longer progress.
    """
    with session_scope() as s:
        search = s.get(Search, search_id)
        if search is not None:
            search.status = str(SearchStatus.ERROR)
            search.completed_at = _now()


def reconcile_search_readiness() -> int:
    """Flip 'enriching' searches whose linked jobs are all terminal (done/skipped/error)
    to 'ready'. A search with no linked jobs (zero results) is ready immediately.
    Returns how many searches were flipped. Cheap; called after each job completion.
    """
    with session_scope() as s:
        has_pending = (
            select(SearchJob.search_id)
            .join(Job, Job.id == SearchJob.job_id)
            .where(Job.status.in_([str(JobStatus.QUEUED), str(JobStatus.RUNNING)]))
        )
        stale = s.scalars(
            select(Search).where(
                Search.status == str(SearchStatus.ENRICHING),
                Search.id.not_in(has_pending),
            )
        ).all()
        for search in stale:
            search.status = str(SearchStatus.READY)
            search.completed_at = _now()
        return len(stale)


def claim_next() -> dict | None:
    """Atomically claim the next runnable job (queued, past its not_before). Returns a
    detached dict snapshot so the caller can run outside the DB transaction.

    Discovery jumps the queue. It is the only job type someone is actually waiting on —
    until it runs, their search sits at 'pending' with an empty screen — whereas
    enrichment and history only fill in columns of results already on screen. Under strict
    FIFO one broad search (a thousand repos, several thousand follow-up jobs) starves the
    next search for many minutes, which is indistinguishable from the app being hung.
    Ordering stays FIFO within each class, so nothing is skipped, only reordered.
    """
    priority = case((Job.type == str(JobType.DISCOVERY), 0), else_=1)
    with session_scope() as s:
        stmt = (
            select(Job)
            .where(
                Job.status == str(JobStatus.QUEUED),
                (Job.not_before.is_(None)) | (Job.not_before <= _now()),
            )
            .order_by(priority, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        try:
            job = s.scalar(stmt)
        except Exception:
            # SQLite has no SKIP LOCKED; fall back to a plain claim under WAL.
            job = s.scalar(stmt.with_for_update(skip_locked=False))
        if job is None:
            return None
        job.status = str(JobStatus.RUNNING)
        job.attempts += 1
        job.updated_at = _now()
        return {
            "id": job.id,
            "type": job.type,
            "dedup_key": job.dedup_key,
            "payload": json.loads(job.payload),
            "attempts": job.attempts,
        }


def complete(job_id: int, status: JobStatus, *, error: str | None = None,
             retry_after_seconds: float | None = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        job.status = str(status)
        job.last_error = error
        job.updated_at = _now()
        if status == JobStatus.ERROR and retry_after_seconds is not None:
            from datetime import timedelta

            job.status = str(JobStatus.QUEUED)  # requeue with backoff
            job.not_before = _now() + timedelta(seconds=retry_after_seconds)
