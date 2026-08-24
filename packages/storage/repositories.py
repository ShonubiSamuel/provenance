"""Repository interfaces + SQLAlchemy-backed implementation.

The rest of the app depends on these Protocols, never on SQLAlchemy directly. Swapping
SQLite for Postgres means providing new implementations bound to a Postgres engine; the
indexer and API code is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Select, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from packages.storage.orm import (
    AssetHistory,
    Match,
    RepoLanguage,
    Repository,
    Search,
    SearchResult,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #
class RepoStore(Protocol):
    def upsert_shallow(self, *, github_id: int, full_name: str) -> Repository: ...
    def upsert_metadata(self, github_id: int, **fields: Any) -> Repository: ...
    def get_by_full_name(self, full_name: str) -> Repository | None: ...
    def needs_enrichment(self, repo: Repository, pushed_at: datetime | None) -> bool: ...


class MatchStore(Protocol):
    def upsert(self, *, repo_id: int, path: str, **fields: Any) -> Match: ...


class SearchStore(Protocol):
    def create(self, *, keyword: str, normalized_query: str, search_type: str) -> Search: ...
    def attach_result(self, search_id: int, match_id: int, rank: int) -> None: ...
    def set_status(self, search_id: int, status: str, **fields: Any) -> None: ...


class AssetHistoryStore(Protocol):
    def upsert(self, *, repo_id: int, path: str, method: str, **fields: Any) -> AssetHistory: ...


# --------------------------------------------------------------------------- #
# SQLAlchemy implementation (SQLite adapter)
# --------------------------------------------------------------------------- #
class SqlRepoStore:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert_shallow(self, *, github_id: int, full_name: str) -> Repository:
        repo = self.get_by_full_name(full_name)
        if repo is None:
            owner, _, name = full_name.partition("/")
            repo = Repository(github_id=github_id, full_name=full_name, owner=owner, name=name)
            self.s.add(repo)
            self.s.flush()
        return repo

    def upsert_metadata(self, github_id: int, **fields: Any) -> Repository:
        repo = self.s.scalar(select(Repository).where(Repository.github_id == github_id))
        if repo is None:
            repo = Repository(github_id=github_id, **_repo_identity(fields))
            self.s.add(repo)
        languages = fields.pop("languages", None)
        for key, value in fields.items():
            setattr(repo, key, value)
        repo.last_enriched_at = _now()
        if repo.pushed_at is not None:
            repo.pushed_at_at_last_index = repo.pushed_at
        self.s.flush()
        if languages is not None:
            self._replace_languages(repo, languages)
        return repo

    def _replace_languages(self, repo: Repository, languages: dict[str, int]) -> None:
        self.s.query(RepoLanguage).filter(RepoLanguage.repo_id == repo.id).delete()
        for lang, byte_count in languages.items():
            self.s.add(RepoLanguage(repo_id=repo.id, language=lang, bytes=byte_count))
        self.s.flush()

    def get_by_full_name(self, full_name: str) -> Repository | None:
        return self.s.scalar(select(Repository).where(Repository.full_name == full_name))

    def needs_enrichment(self, repo: Repository, pushed_at: datetime | None) -> bool:
        """Skip enrichment when the repo hasn't been pushed since we last indexed it."""
        if repo.last_enriched_at is None:
            return True
        if pushed_at is None or repo.pushed_at_at_last_index is None:
            return True
        # SQLite round-trips DateTime(timezone=True) as naive, even for values that were
        # written tz-aware — normalize both sides so the comparison can't raise.
        last_index = repo.pushed_at_at_last_index
        if last_index.tzinfo is None:
            last_index = last_index.replace(tzinfo=timezone.utc)
        if pushed_at.tzinfo is None:
            pushed_at = pushed_at.replace(tzinfo=timezone.utc)
        return pushed_at > last_index


class SqlMatchStore:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert(self, *, repo_id: int, path: str, **fields: Any) -> Match:
        match = self.s.scalar(
            select(Match).where(Match.repo_id == repo_id, Match.path == path)
        )
        if match is None:
            filename = path.rsplit("/", 1)[-1]
            match = Match(repo_id=repo_id, path=path, filename=filename)
            self.s.add(match)
        for key, value in fields.items():
            setattr(match, key, value)
        if match.path_prefix is None:
            match.path_prefix = _top_prefix(path)
        if match.extension is None and "." in match.filename:
            match.extension = "." + match.filename.rsplit(".", 1)[-1]
        self.s.flush()
        return match


class SqlSearchStore:
    def __init__(self, session: Session) -> None:
        self.s = session

    def create(self, *, keyword: str, normalized_query: str, search_type: str) -> Search:
        search = Search(
            keyword=keyword,
            normalized_query=normalized_query,
            search_type=search_type,
            status="pending",
            created_at=_now(),
        )
        self.s.add(search)
        self.s.flush()
        return search

    def attach_result(self, search_id: int, match_id: int, rank: int) -> None:
        exists = self.s.get(SearchResult, {"search_id": search_id, "match_id": match_id})
        if exists is None:
            self.s.add(SearchResult(search_id=search_id, match_id=match_id, rank=rank))

    def set_status(self, search_id: int, status: str, **fields: Any) -> None:
        search = self.s.get(Search, search_id)
        if search is None:
            return
        search.status = status
        for key, value in fields.items():
            setattr(search, key, value)
        self.s.flush()


class SqlAssetHistoryStore:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert(self, *, repo_id: int, path: str, method: str, **fields: Any) -> AssetHistory:
        row = self.s.scalar(
            select(AssetHistory).where(
                AssetHistory.repo_id == repo_id,
                AssetHistory.path == path,
                AssetHistory.method == method,
            )
        )
        if row is None:
            row = AssetHistory(repo_id=repo_id, path=path, method=method)
            self.s.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        row.computed_at = _now()
        self.s.flush()
        return row


# --------------------------------------------------------------------------- #
# Facets (read path) — one column map is the single source of truth for BOTH the
# facet groups shown in the sidebar and the filter query-params accepted by /search,
# so the two can never drift apart. Group name == facet key == query-param name.
# --------------------------------------------------------------------------- #
FACET_COLUMNS: dict[str, InstrumentedAttribute] = {
    "languages": Match.detected_language,
    "extensions": Match.extension,
    "path_prefixes": Match.path_prefix,
    "owners": Repository.owner,
    "licenses": Repository.license_spdx,
}


def compute_facets(
    session: Session,
    search_id: int,
    *,
    filters: dict[str, list[str]] | None = None,
    collapse_forks: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Auto-discover filters from a search's results: languages, path prefixes, owners,
    licenses. This is what populates the left sidebar without the user knowing any syntax.

    Counts are **cross-filtered**: each group's counts reflect every OTHER active facet
    filter (and the fork toggle) but NOT that group's own selection. So counts narrow as
    you filter on other groups, yet a group you've already filtered on still shows all its
    sibling values with live counts — the standard "add more from this group" behaviour.
    """
    filters = filters or {}

    def _grouped(group: str, column: InstrumentedAttribute) -> list[dict[str, Any]]:
        # Everything except this group's own selection, so its options stay listed.
        others = {g: v for g, v in filters.items() if g != group}
        stmt = (
            select(column, func.count().label("n"))
            .select_from(SearchResult)
            .join(Match, Match.id == SearchResult.match_id)
            .join(Repository, Repository.id == Match.repo_id)
            .where(SearchResult.search_id == search_id, column.is_not(None))
        )
        if collapse_forks:
            stmt = stmt.where(Repository.is_fork.is_(False))
        stmt = apply_facet_filters(stmt, others)
        stmt = stmt.group_by(column).order_by(func.count().desc())
        return [{"value": v, "count": n} for v, n in session.execute(stmt).all()]

    return {group: _grouped(group, column) for group, column in FACET_COLUMNS.items()}


def apply_facet_filters(stmt: Select, filters: dict[str, list[str]]) -> Select:
    """AND a `column IN (values)` clause per non-empty facet group onto a results query.

    Within a group the values are OR'd (IN); across groups they are AND'd (successive
    WHEREs) — matching the sidebar's multi-select semantics. Unknown group names are
    ignored so a stale client param can't 500 the endpoint. The caller must already have
    joined `Match` and `Repository` (every column in FACET_COLUMNS lives on one of them).
    """
    for group, values in filters.items():
        column = FACET_COLUMNS.get(group)
        if column is not None and values:
            stmt = stmt.where(column.in_(values))
    return stmt


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _top_prefix(path: str, depth: int = 2) -> str:
    """Directory prefix used for the 'Common Paths' facet and asset-history granularity."""
    parts = path.split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[: min(depth, len(parts) - 1)])


def _repo_identity(fields: dict[str, Any]) -> dict[str, Any]:
    full_name = fields.get("full_name", "")
    owner, _, name = full_name.partition("/")
    return {"full_name": full_name, "owner": owner, "name": name}
