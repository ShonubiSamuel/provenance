"""API request/response models (Pydantic)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from packages.core.enums import SearchType


class IndexRequest(BaseModel):
    keyword: str
    # AUTO by default: the backend classifies the input. A concrete type here is the
    # user overriding that guess from the UI.
    search_type: SearchType = SearchType.AUTO


class IndexResponse(BaseModel):
    search_id: int
    status: str
    # What AUTO resolved to, so the UI can show what it actually ran.
    search_type: str = "keyword"
    normalized_query: str = ""
    explanation: str = ""


class DetectResponse(BaseModel):
    """Preview of how an input will be interpreted — same code path as /index."""

    search_type: str
    query: str
    normalized_query: str
    explanation: str
    repo: str | None = None
    path: str | None = None
    ref: str | None = None


class BucketStatus(BaseModel):
    rate_per_minute: int
    tokens: float
    parked_seconds: float
    wait_seconds: float


class StatusResponse(BaseModel):
    """Health of everything the user cannot see: credentials, GitHub's behaviour, and
    our own pacing. Polled by the UI so a stalled search always has a stated reason."""

    credential: str  # github-app | personal-access-token | none
    credential_ok: bool
    token_expires_at: str | None = None
    search_limit: int | None = None
    search_remaining: int | None = None
    search_reset_in: int | None = None  # seconds
    github_degraded: bool = False
    last_error: str | None = None
    buckets: dict[str, BucketStatus] = {}
    message: str | None = None  # null when there is nothing wrong


class ResultRow(BaseModel):
    repo_full_name: str
    owner: str
    stars: int | None
    license_spdx: str | None
    path: str
    filename: str
    extension: str | None
    path_prefix: str | None
    detected_language: str | None
    snippet: str | None
    repo_created_at: datetime | None
    repo_pushed_at: datetime | None
    asset_first_appeared_at: datetime | None
    history_method: str | None
    github_url: str


class SearchResponse(BaseModel):
    search_id: int
    keyword: str
    search_type: str
    normalized_query: str = ""
    status: str
    truncated: bool
    sampled: bool = False
    total_matches: int          # what we hold locally
    reported_matches: int = 0   # what GitHub claimed existed
    note: str | None = None     # why the outcome is imperfect, in plain words
    results: list[ResultRow]


class FacetsResponse(BaseModel):
    search_id: int
    facets: dict[str, list[dict]]


class RecentSearch(BaseModel):
    search_id: int
    keyword: str
    search_type: str
    status: str
    truncated: bool
    total_matches: int
    reported_matches: int = 0
    note: str | None = None
    created_at: datetime | None


class RecentSearchesResponse(BaseModel):
    searches: list[RecentSearch]


# --- Repo inspection (on-demand, live GitHub) --- #
class TreeEntry(BaseModel):
    name: str
    path: str
    type: str  # "dir" | "file"
    size: int | None = None
    sha: str | None = None


class TreeResponse(BaseModel):
    owner: str
    repo: str
    ref: str | None
    path: str
    entries: list[TreeEntry]


class BlobResponse(BaseModel):
    owner: str
    repo: str
    path: str
    size: int
    encoding: str  # "text" | "binary" | "too_large"
    text: str | None = None


class SizesResponse(BaseModel):
    owner: str
    repo: str
    ref: str | None
    truncated: bool
    sizes: dict[str, int]  # repo-relative folder path -> total bytes beneath it


# --- Download manager --- #
class DownloadRequest(BaseModel):
    kind: str  # "file" | "repo" | "folder"
    owner: str
    repo: str
    path: str | None = None  # required for kind="file" and kind="folder"
    ref: str | None = None


class DownloadItem(BaseModel):
    id: int
    kind: str
    label: str
    status: str  # waiting | active | paused | complete | error | removed
    total_bytes: int
    total_is_estimate: bool = False  # repo zips: pre-estimated from tree blob sizes
    completed_bytes: int
    speed_bytes: int
    progress: float
    file_count: int | None = None  # folder fan-outs only
    error: str | None = None


class DownloadsResponse(BaseModel):
    available: bool
    download_dir: str
    free_bytes: int | None = None
    items: list[DownloadItem]


class DownloadFile(BaseModel):
    path: str
    size: int
    status: str
    completed_bytes: int


class DownloadFilesResponse(BaseModel):
    id: int
    files: list[DownloadFile]
