"""Persistent download history + reconciliation against the aria2 engine.

The SQLite `downloads` table is the source of truth for the UI: rows survive app
restarts and aria2's own purging of finished results. Each poll, `reconcile_and_list`
folds aria2's live per-GID status into the non-terminal rows (aggregating folder
fan-outs into one row) and returns UI-ready items, newest first.

Children are stored as JSON: {gid, path, size, dest, url}. `dest` (relative to the
download dir) lets us resolve a GID aria2 no longer knows HONESTLY from disk: the file
present without its `.aria2` control sidecar means aria2 finished it and purged the
result; anything else means the engine lost it (ungraceful kill before a session save)
and the row goes to `error` so the user can Retry — we never silently claim success.
`url` lets a folder retry re-enqueue exactly the missing files (raw URLs are pinned to
a commit, so they stay valid).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from packages.downloads.manager import DownloadManager, DownloadView
from packages.storage.db import session_scope
from packages.storage.orm import Download

# Statuses that never change again — reconcile skips these rows.
TERMINAL = {"complete", "error", "removed"}
_HISTORY_LIMIT = 200

_LOST_MSG = "interrupted — the engine lost this download; press Retry"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_download(
    *,
    kind: str,
    label: str,
    owner: str,
    repo: str,
    path: str | None,
    children: list[dict[str, Any]],
    total_bytes: int = 0,
    total_is_estimate: bool = False,
) -> int:
    with session_scope() as s:
        row = Download(
            kind=kind, label=label, owner=owner, repo=repo, path=path,
            children=json.dumps(children), status="waiting",
            total_bytes=total_bytes, total_is_estimate=total_is_estimate,
            created_at=_now(), updated_at=_now(),
        )
        s.add(row)
        s.flush()
        return row.id


def get_download(download_id: int) -> dict[str, Any] | None:
    """Row snapshot for the retry endpoint (kind, coordinates, children)."""
    with session_scope() as s:
        row = s.get(Download, download_id)
        if row is None:
            return None
        return {
            "id": row.id, "kind": row.kind, "owner": row.owner, "repo": row.repo,
            "path": row.path, "children": json.loads(row.children),
            "status": row.status,
        }


def replace_children(
    download_id: int,
    children: list[dict[str, Any]],
    *,
    total_bytes: int | None = None,
    total_is_estimate: bool = False,
) -> None:
    """Swap in a new child set after a retry and put the row back in flight."""
    with session_scope() as s:
        row = s.get(Download, download_id)
        if row is None:
            return
        row.children = json.dumps(children)
        row.status = "waiting"
        row.error = None
        if total_bytes is not None:
            row.total_bytes = total_bytes
            row.total_is_estimate = total_is_estimate
        row.completed_bytes = 0
        row.updated_at = _now()


def child_complete_on_disk(child: dict[str, Any], base_dir: Path) -> bool:
    """A finished child leaves its file WITHOUT the `.aria2` control sidecar."""
    dest = child.get("dest")
    if not dest:
        return False
    target = base_dir / dest
    return target.exists() and not Path(f"{target}.aria2").exists()


def _resolve_missing(child: dict[str, Any], base_dir: Path) -> tuple[str, int]:
    """Status + completed bytes for a child aria2 no longer tracks."""
    dest = child.get("dest")
    size = int(child.get("size") or 0)
    if not dest:
        # Legacy rows (before dest was recorded): keep the old optimistic assumption.
        return "complete", size
    target = base_dir / dest
    if child_complete_on_disk(child, base_dir):
        return "complete", size or target.stat().st_size
    return "error", 0


def _aggregate(
    children: list[dict[str, Any]],
    views: dict[str, DownloadView],
    base_dir: Path,
) -> tuple[str, int, int, int, str | None]:
    """Fold child GID states into (status, total, completed, speed, error)."""
    total = completed = speed = 0
    statuses: list[str] = []
    error: str | None = None
    for child in children:
        view = views.get(child["gid"])
        size = int(child.get("size") or 0)
        if view is None:
            child_status, done = _resolve_missing(child, base_dir)
            total += size or done
            completed += done
            statuses.append(child_status)
            if child_status == "error" and error is None:
                error = _LOST_MSG
            continue
        child_total = view.total_bytes or size
        total += child_total
        completed += min(view.completed_bytes, child_total) if child_total else view.completed_bytes
        speed += view.speed_bytes
        statuses.append(view.status)
        if view.status == "error" and error is None:
            error = view.error

    if "active" in statuses:
        status = "active"
    elif "paused" in statuses:
        status = "paused"
    elif "waiting" in statuses:
        status = "waiting"
    elif "error" in statuses:
        status = "error"
    else:
        status = "complete"
    return status, total, completed, speed, error


def reconcile_and_list(manager: DownloadManager | None) -> list[dict[str, Any]]:
    """Update non-terminal rows from aria2's live state and return all history rows as
    UI items (newest first). Works with the manager unavailable (rows just don't move).
    """
    engine_up = manager is not None and manager.available
    views = manager.views_by_gid() if engine_up else {}
    base_dir = manager.download_dir if manager is not None else Path(".")
    items: list[dict[str, Any]] = []
    with session_scope() as s:
        rows = s.scalars(
            select(Download).order_by(Download.id.desc()).limit(_HISTORY_LIMIT)
        ).all()
        for row in rows:
            speed = 0
            # Reconcile whenever the engine is up — an EMPTY view map is meaningful
            # (everything finished and was purged), not a reason to skip.
            if row.status not in TERMINAL and engine_up:
                children = json.loads(row.children)
                status, total, completed, speed, error = _aggregate(
                    children, views, base_dir
                )
                # Codeload zipballs stream chunked with NO Content-Length, so `total`
                # stays 0 while `completed` grows — always surface live completed
                # bytes, and never shrink a previously-known total back to 0. A real
                # engine-reported total replaces a pre-computed estimate for good.
                if total:
                    row.total_bytes = total
                    row.total_is_estimate = False
                if completed:
                    row.completed_bytes = completed
                elif status == "complete" and row.total_bytes:
                    row.completed_bytes = row.total_bytes
                row.status = status
                row.error = error
                row.updated_at = _now()
            items.append(_item(row, speed))
    return items


def list_files(
    download_id: int, manager: DownloadManager | None
) -> list[dict[str, Any]] | None:
    """Per-child status for one download (the expandable detail view). None if no row."""
    engine_up = manager is not None and manager.available
    views = manager.views_by_gid() if engine_up else {}
    base_dir = manager.download_dir if manager is not None else Path(".")
    with session_scope() as s:
        row = s.get(Download, download_id)
        if row is None:
            return None
        children = json.loads(row.children)
    out = []
    for child in children:
        view = views.get(child["gid"])
        if view is not None:
            status, completed = view.status, view.completed_bytes
        elif engine_up:
            status, completed = _resolve_missing(child, base_dir)
        else:
            status, completed = "unknown", 0
        out.append({
            "path": child.get("path") or child.get("dest") or "",
            "size": int(child.get("size") or 0),
            "status": status,
            "completed_bytes": completed,
        })
    return out


def child_gids(download_id: int) -> list[str]:
    with session_scope() as s:
        row = s.get(Download, download_id)
        if row is None:
            return []
        return [c["gid"] for c in json.loads(row.children)]


def mark_status(download_id: int, status: str) -> None:
    with session_scope() as s:
        row = s.get(Download, download_id)
        if row is not None:
            row.status = status
            row.updated_at = _now()


def purge_finished() -> int:
    """Delete terminal rows (complete/error/removed) from history. Returns count."""
    with session_scope() as s:
        result = s.execute(
            delete(Download).where(Download.status.in_(sorted(TERMINAL)))
        )
        return result.rowcount or 0


def _item(row: Download, speed: int) -> dict[str, Any]:
    file_count = len(json.loads(row.children)) if row.kind == "folder" else None
    progress = (row.completed_bytes / row.total_bytes) if row.total_bytes else 0.0
    return {
        "id": row.id,
        "kind": row.kind,
        "label": row.label,
        "status": row.status,
        "total_bytes": row.total_bytes,
        "total_is_estimate": row.total_is_estimate,
        "completed_bytes": row.completed_bytes,
        "speed_bytes": speed,
        # An estimated total can undershoot the real zip — never show >100%.
        "progress": min(1.0, progress),
        "file_count": file_count,
        "error": row.error,
    }
