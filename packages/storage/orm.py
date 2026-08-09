"""SQLAlchemy 2.0 ORM models — a 1:1 mapping of docs/DATA_MODEL_AND_PIPELINE.md.

Column names and semantics match the design doc exactly. Postgres portability: we avoid
SQLite-only column types here; FTS5 lives in a separate module because it is engine
specific and gets swapped for a tsvector/pg_trgm implementation under Postgres.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repository(Base):
    """Global, deduplicated repo cache — enriched once, reused across searches."""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String, unique=True, index=True)
    owner: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    owner_type: Mapped[str | None] = mapped_column(String, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    stars: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    forks: Mapped[int | None] = mapped_column(Integer, default=None)
    primary_language: Mapped[str | None] = mapped_column(String, default=None, index=True)
    license_spdx: Mapped[str | None] = mapped_column(String, default=None, index=True)

    is_fork: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_full_name: Mapped[str | None] = mapped_column(String, default=None, index=True)
    default_branch: Mapped[str | None] = mapped_column(String, default=None)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Detector output + refresh bookkeeping
    unity_version: Mapped[str | None] = mapped_column(String, default=None, index=True)
    etag: Mapped[str | None] = mapped_column(String, default=None)
    pushed_at_at_last_index: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    languages: Mapped[list["RepoLanguage"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    matches: Mapped[list["Match"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )


class RepoLanguage(Base):
    __tablename__ = "repo_languages"

    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0)

    repo: Mapped[Repository] = relationship(back_populates="languages")


class Match(Base):
    """A matched file in a repo — universal, not per-search."""

    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("repo_id", "path", name="uq_match_repo_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    path: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String, index=True)
    extension: Mapped[str | None] = mapped_column(String, default=None, index=True)
    detected_language: Mapped[str | None] = mapped_column(String, default=None, index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    content_sha: Mapped[str | None] = mapped_column(String, default=None)
    snippet: Mapped[str | None] = mapped_column(Text, default=None)
    path_prefix: Mapped[str | None] = mapped_column(String, default=None, index=True)

    repo: Mapped[Repository] = relationship(back_populates="matches")


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String, index=True)
    normalized_query: Mapped[str] = mapped_column(String)
    search_type: Mapped[str] = mapped_column(String, default="keyword")
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # What we actually hold locally…
    total_matches: Mapped[int] = mapped_column(Integer, default=0)
    # …versus what GitHub claimed existed. Keeping both is what lets the UI say
    # "812 collected of ≈1,304 reported" instead of picking one and misleading.
    reported_matches: Mapped[int] = mapped_column(Integer, default=0)
    # True when the query was too broad to collect in full and we kept a sample.
    sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Human-readable explanation of an unhappy outcome (degraded GitHub, rate limit,
    # expired token, sampled query). Shown verbatim in the UI; null when all is well.
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    results: Mapped[list["SearchResult"]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )


class SearchResult(Base):
    __tablename__ = "search_results"

    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id"), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)

    search: Mapped[Search] = relationship(back_populates="results")


class AssetHistory(Base):
    """The headline feature — keyed by (repo, path, method) so both tiers coexist."""

    __tablename__ = "asset_history"
    __table_args__ = (
        UniqueConstraint("repo_id", "path", "method", name="uq_history_repo_path_method"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    path: Mapped[str] = mapped_column(String, index=True)
    first_appeared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    first_commit_sha: Mapped[str | None] = mapped_column(String, default=None)
    last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    method: Mapped[str] = mapped_column(String, default="api-approx")
    follows_renames: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SearchJob(Base):
    """Links a search to the jobs its discovery emitted (including dedup-hits on jobs
    another search created). This is what lets readiness reconciliation answer 'are all
    of THIS search's jobs terminal?' even though jobs themselves are global."""

    __tablename__ = "search_jobs"

    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id"), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), primary_key=True)


class Download(Base):
    """Persistent download history — one row per user-initiated download (a single file,
    a whole repo, or a folder fan-out). `children` is a JSON list of
    {gid, path, size} entries; a folder download has many, file/repo have one. The row
    outlives aria2's own bookkeeping, so history survives restarts and engine purges.
    """

    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)  # file | repo | folder
    label: Mapped[str] = mapped_column(String)
    owner: Mapped[str] = mapped_column(String)
    repo: Mapped[str] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(String, default=None)
    children: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String, default="waiting", index=True)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # True while total_bytes is an estimate (repo zips: GitHub streams them with no
    # Content-Length, so we pre-estimate from the tree's blob sizes). Cleared once the
    # engine reports the real total.
    total_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String, index=True)
    dedup_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
