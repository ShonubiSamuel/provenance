"""The four pipeline stages, wired to storage + GitHub client.

Discovery is hardened (query-splitting past 1000 + snippet capture, see discovery.py);
enrichment/history run against real endpoints; detectors remain a registered stub. Each
stage is idempotent: re-running with the same inputs converges to the same rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from dateutil import parser as dtparse

from packages.core.enums import HistoryMethod, JobType, SearchStatus
from packages.core.settings import get_settings
from packages.github.client import GitHubClient, GitHubError
from packages.indexer.discovery import DiscoveryEngine
from packages.storage.db import session_scope
from packages.storage.orm import Search
from packages.storage.repositories import (
    SqlAssetHistoryStore,
    SqlMatchStore,
    SqlRepoStore,
    SqlSearchStore,
)
from packages.indexer.queue import enqueue, link_search_job, reconcile_search_readiness

log = logging.getLogger("indexer.stages")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = dtparse.isoparse(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _finish_unhappy(search_id: int, note: str) -> None:
    """Park a search in `error` with an explanation the UI shows verbatim. Used for
    every outcome the user can act on (bad token, GitHub down, degraded search) so the
    status is never just a spinner that stops moving."""
    with session_scope() as s:
        if s.get(Search, search_id) is not None:
            SqlSearchStore(s).set_status(
                search_id, str(SearchStatus.ERROR), note=note, completed_at=_utcnow()
            )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _discovery_note(stats) -> str | None:
    """One sentence explaining an imperfect collection — or None when it went fine.
    Order matters: report the most consequential problem first."""
    if stats.degraded:
        return (
            f"GitHub reported ≈{stats.total_reported:,} matches but returned none. Its "
            f"code search is degraded or throttling this token right now — "
            f"{stats.collected:,} results were stored before it stopped answering. Try "
            f"again in a few minutes."
        )
    if stats.sampled:
        return (
            f"≈{stats.total_reported:,} matches is far too many to index — GitHub only "
            f"serves 1,000 per query and caps code search at 10 calls/minute. Stored the "
            f"first {stats.collected:,}. Narrow the search (a path, filename or "
            f"extension) for complete results."
        )
    if stats.hit_call_budget:
        return (
            f"Stopped at the {stats.search_calls}-call budget for one search with "
            f"{stats.collected:,} of ≈{stats.total_reported:,} results. Narrow the query "
            f"to collect the rest."
        )
    if stats.truncated:
        return (
            "Some size bands still exceeded GitHub's 1,000-result ceiling, so part of "
            "the tail is missing. Facet counts are biased toward what was collected."
        )
    return None


# --------------------------------------------------------------------------- #
# Stage 1 — Discovery
# --------------------------------------------------------------------------- #
async def run_discovery(gh: GitHubClient, payload: dict) -> None:
    """Collect all matches (splitting past the 1000 ceiling), persisting each page AS IT
    ARRIVES — results reach the UI within seconds of the first page instead of after the
    whole (potentially many-minute) collection. Emits one enrichment job per repo and
    one history job per (repo, path_prefix), deduped across the stream.
    """
    search_id = payload["search_id"]
    query = payload["normalized_query"]

    with session_scope() as s:
        SqlSearchStore(s).set_status(search_id, str(SearchStatus.DISCOVERING), note=None)

    def progress(stats) -> bool:
        """Heartbeat before every GitHub call: stream progress to the UI, and cancel
        the collection if the user deleted the search meanwhile. `collected` and
        `reported` are stored separately — a collection that has stored 0 of a reported
        1,304 is a very different thing from one that found 1,304, and the UI must be
        able to tell them apart while it is still running."""
        with session_scope() as s:
            row = s.get(Search, search_id)
            if row is None:
                return False
            row.total_matches = stats.collected
            row.reported_matches = stats.total_reported
            return True

    seen_repos: set[int] = set()
    seen_prefixes: set[tuple[int, str]] = set()
    rank = 0

    def persist(batch) -> None:
        """Streaming sink: upsert one absorbed page and emit its follow-up jobs. Fully
        idempotent (upserts + get-before-add attach), so a job retry converges. Ranks
        follow collection order (relevance is only globally meaningful when no size
        split occurred — see the design doc caveat)."""
        nonlocal rank
        # Collected inside the txn, enqueued after it: enqueue/link open their own
        # sessions, and nesting a second write session inside this one risks SQLite
        # write-lock contention.
        to_emit: list[tuple[JobType, str, dict]] = []
        with session_scope() as s:
            if s.get(Search, search_id) is None:
                return  # search deleted mid-run; the heartbeat will cancel shortly
            repo_store, match_store, search_store = (
                SqlRepoStore(s), SqlMatchStore(s), SqlSearchStore(s)
            )
            for f in batch:
                rank += 1
                repo = repo_store.upsert_shallow(
                    github_id=f.repo_github_id, full_name=f.repo_full_name
                )
                match = match_store.upsert(
                    repo_id=repo.id,
                    path=f.path,
                    detected_language=f.detected_language,
                    extension=f.extension,
                    content_sha=f.sha,
                    snippet=f.snippet,
                    path_prefix=f.path_prefix,
                )
                search_store.attach_result(search_id, match.id, rank)

                if repo.id not in seen_repos:
                    seen_repos.add(repo.id)
                    observed_pushed_at = _parse_dt(f.repo_pushed_at)
                    if repo_store.needs_enrichment(repo, observed_pushed_at):
                        # Keyed on the observed pushed_at (not just repo.id) so a repo
                        # that has genuinely moved since its last enrichment gets a
                        # fresh job — enqueue()'s dedup_key is otherwise permanent and
                        # would silently no-op every re-discovery forever.
                        to_emit.append((
                            JobType.REPO_ENRICHMENT,
                            f"enrich:{repo.id}:{f.repo_pushed_at or 'unknown'}",
                            {"repo_id": repo.id},
                        ))
                prefix_key = (repo.id, f.path_prefix)
                if prefix_key not in seen_prefixes:
                    seen_prefixes.add(prefix_key)
                    to_emit.append((
                        JobType.ASSET_HISTORY,
                        f"history:approx:{repo.id}:{f.path_prefix}",
                        {"repo_id": repo.id, "owner": repo.owner, "repo": repo.name,
                         "path": f.path_prefix},
                    ))
        for job_type, dedup_key, job_payload in to_emit:
            job_id = enqueue(job_type, dedup_key, job_payload)
            link_search_job(search_id, job_id)
        log.info(
            "persisted %d results (rank→%d, %d follow-up jobs) search=%s",
            len(batch), rank, len(to_emit), search_id,
        )

    settings = get_settings()
    try:
        _files, stats = await DiscoveryEngine(
            gh,
            call_budget=settings.discovery_call_budget,
            sample_above=settings.discovery_sample_above,
            progress_cb=progress,
            on_files=persist,
        ).run(query)
    except GitHubError as exc:
        # An expired token, a hard 403, or a code-search backend that will not serve us.
        # Retrying five times changes none of those, so record WHY and stop — a search
        # that can't progress must say so rather than spin.
        log.warning("discovery search=%s aborted: %s", search_id, exc.user_message)
        _finish_unhappy(search_id, exc.user_message)
        return

    if stats.cancelled:
        log.info("discovery search=%s cancelled (search deleted) after %d calls",
                 search_id, stats.search_calls)
        return
    log.info(
        "discovery search=%s query=%r reported=%d collected=%d calls=%d "
        "split=%s truncated=%s sampled=%s degraded=%s bands=%s budget_hit=%s",
        search_id, query, stats.total_reported, stats.collected, stats.search_calls,
        stats.size_split_used, stats.truncated, stats.sampled, stats.degraded,
        stats.truncated_bands, stats.hit_call_budget,
    )

    if stats.total_reported == 0 and stats.collected == 0 and not stats.degraded:
        # "No matches" and "the index isn't answering" are the same response. Spend one
        # canary call to find out which, rather than telling the user their search term
        # is wrong when GitHub is the one at fault.
        if not await gh.code_search_alive():
            _finish_unhappy(
                search_id,
                "GitHub's code search returned no results for anything — including a "
                "control query that always matches. Its search index is degraded right "
                "now, so this is not a statement about your search. Try again shortly.",
            )
            return

    note = _discovery_note(stats)
    if stats.degraded and stats.collected == 0:
        # Nothing stored and GitHub is refusing to serve results: this is a failure, not
        # an empty result set, and must not be presented as "0 matches".
        _finish_unhappy(search_id, note)
        return

    with session_scope() as s:
        if s.get(Search, search_id) is not None:
            SqlSearchStore(s).set_status(
                search_id,
                str(SearchStatus.ENRICHING),
                truncated=stats.truncated,
                total_matches=stats.collected,
                reported_matches=stats.total_reported,
                sampled=stats.sampled,
                note=note,
            )
    # Zero-result searches, or ones whose jobs were all dedup-hits that already finished,
    # are ready right now — workers won't fire again for them.
    reconcile_search_readiness()


# --------------------------------------------------------------------------- #
# Stage 2 — Repo enrichment
# --------------------------------------------------------------------------- #
_REPO_GQL = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    databaseId stargazerCount forkCount isFork isArchived
    createdAt pushedAt updatedAt
    description
    owner { __typename }
    defaultBranchRef { name }
    licenseInfo { spdxId }
    primaryLanguage { name }
    parent { nameWithOwner }
    languages(first: 20, orderBy:{field:SIZE, direction:DESC}) {
      edges { size node { name } }
    }
  }
}
"""


async def run_repo_enrichment(gh: GitHubClient, payload: dict) -> None:
    repo_id = payload["repo_id"]
    with session_scope() as s:
        from packages.storage.orm import Repository

        repo = s.get(Repository, repo_id)
        if repo is None:
            return
        owner, name, github_id = repo.owner, repo.name, repo.github_id

    resp = await gh.graphql(_REPO_GQL, {"owner": owner, "name": name})
    node = (resp.get("data") or {}).get("repository")
    if not node:
        return

    languages = {
        edge["node"]["name"]: edge["size"] for edge in node["languages"]["edges"]
    }
    with session_scope() as s:
        SqlRepoStore(s).upsert_metadata(
            github_id,
            full_name=f"{owner}/{name}",
            description=node.get("description"),
            stars=node.get("stargazerCount"),
            forks=node.get("forkCount"),
            is_fork=node.get("isFork", False),
            is_archived=node.get("isArchived", False),
            owner_type=(node.get("owner") or {}).get("__typename"),
            parent_full_name=(node.get("parent") or {}).get("nameWithOwner"),
            default_branch=(node.get("defaultBranchRef") or {}).get("name"),
            license_spdx=(node.get("licenseInfo") or {}).get("spdxId"),
            primary_language=(node.get("primaryLanguage") or {}).get("name"),
            created_at=_parse_dt(node.get("createdAt")),
            pushed_at=_parse_dt(node.get("pushedAt")),
            updated_at=_parse_dt(node.get("updatedAt")),
            languages=languages,
        )
    enqueue(JobType.DETECTOR, f"detect:unity_version:{repo_id}",
            {"repo_id": repo_id, "detector": "unity_version", "owner": owner, "repo": name})


# --------------------------------------------------------------------------- #
# Stage 3 — Asset history (api-approx tier)
# --------------------------------------------------------------------------- #
async def run_asset_history(gh: GitHubClient, payload: dict) -> None:
    bounds = await gh.commit_bounds_for_path(payload["owner"], payload["repo"], payload["path"])
    if bounds is None:
        return
    oldest, newest = bounds
    with session_scope() as s:
        SqlAssetHistoryStore(s).upsert(
            repo_id=payload["repo_id"],
            path=payload["path"],
            method=str(HistoryMethod.API_APPROX),
            first_appeared_at=_parse_dt(oldest["commit"]["committer"]["date"]),
            first_commit_sha=oldest.get("sha"),
            last_modified_at=_parse_dt(newest["commit"]["committer"]["date"]),
            follows_renames=False,
        )


# --------------------------------------------------------------------------- #
# Stage 4 — Detector (dispatches to the plugin registry)
# --------------------------------------------------------------------------- #
async def run_detector(gh: GitHubClient, payload: dict) -> None:
    from packages.detectors import base as detectors  # import triggers registration

    detector = detectors.get(payload.get("detector", ""))
    if detector is None:
        log.warning("no detector registered for %r", payload.get("detector"))
        return

    fields = await detector.run(
        gh, owner=payload["owner"], repo=payload["repo"], repo_id=payload["repo_id"]
    )
    if not fields:
        return
    with session_scope() as s:
        from packages.storage.orm import Repository

        repo = s.get(Repository, payload["repo_id"])
        if repo is None:
            return
        for key, value in fields.items():
            setattr(repo, key, value)


STAGE_DISPATCH = {
    str(JobType.DISCOVERY): run_discovery,
    str(JobType.REPO_ENRICHMENT): run_repo_enrichment,
    str(JobType.ASSET_HISTORY): run_asset_history,
    str(JobType.DETECTOR): run_detector,
}
