"""Shared enums used across storage, indexer, and the API.

Kept as plain string enums so they serialize cleanly to JSON and to TEXT columns.
"""
from __future__ import annotations

from enum import StrEnum


class SearchType(StrEnum):
    # AUTO is the default the UI sends: the input is classified by packages.core.query
    # and resolved to one of the concrete types below. Everything else is the user
    # overriding that guess.
    AUTO = "auto"
    KEYWORD = "keyword"
    PHRASE = "phrase"
    REGEX = "regex"
    PATH = "path"
    FILENAME = "filename"
    EXTENSION = "ext"
    LANGUAGE = "language"
    REPO = "repo"      # the input named one repo — inspect it, don't index the world
    RAW = "raw"        # already GitHub syntax; passed through untouched


class SearchStatus(StrEnum):
    PENDING = "pending"
    DISCOVERING = "discovering"
    ENRICHING = "enriching"
    READY = "ready"
    ERROR = "error"


class JobType(StrEnum):
    DISCOVERY = "discovery"
    REPO_ENRICHMENT = "repo_enrichment"
    ASSET_HISTORY = "asset_history"
    DETECTOR = "detector"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


class HistoryMethod(StrEnum):
    API_APPROX = "api-approx"       # commits-for-path, no rename following
    CLONE_PRECISE = "clone-precise"  # blobless clone + git log --follow


class RateBucket(StrEnum):
    CORE = "core"
    SEARCH = "search"
    GRAPHQL = "graphql"
