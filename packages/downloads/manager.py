"""Backend download manager: supervises one aria2c daemon and drives it over JSON-RPC.

This is the third traffic type's engine (on-demand downloads to the user's disk). We run
a single `aria2c --enable-rpc` process and talk to it with the aria2p Client, whose method
names map 1:1 to aria2's JSON-RPC. The manager owns only the aria2 relationship — callers
(the API endpoints) resolve GitHub URLs and hand us final, ready-to-fetch URLs.

Durability: aria2 is launched with --continue + --save-session + --save-session-interval
and an --input-file pointing at the same session file, so unfinished downloads survive an
app restart or reboot and resume from their .aria2 control files (the 'resume after days'
requirement). Only a graceful shutdown guarantees a final session write.

Degrades gracefully: if the aria2c binary is missing, `available` is False and the API
surfaces a clear "install aria2" message instead of crashing.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aria2p

log = logging.getLogger("downloads.manager")

# Status fields we ask aria2 for — keeps RPC payloads small.
_STATUS_KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "errorCode", "errorMessage", "files", "dir",
]


@dataclass
class DownloadView:
    """UI-facing snapshot of one aria2 download."""

    gid: str
    name: str
    status: str  # active | waiting | paused | complete | error | removed
    total_bytes: int
    completed_bytes: int
    speed_bytes: int
    progress: float  # 0..1
    error: str | None
    path: str | None


def _name_and_path(status: dict[str, Any]) -> tuple[str, str | None]:
    files = status.get("files") or []
    path = files[0].get("path") if files else None
    if path:
        return os.path.basename(path) or path, path
    return status.get("gid", "download"), None


def _to_view(status: dict[str, Any]) -> DownloadView:
    total = int(status.get("totalLength", 0) or 0)
    done = int(status.get("completedLength", 0) or 0)
    name, path = _name_and_path(status)
    err_msg = status.get("errorMessage") or None
    return DownloadView(
        gid=status.get("gid", ""),
        name=name,
        status=status.get("status", "unknown"),
        total_bytes=total,
        completed_bytes=done,
        speed_bytes=int(status.get("downloadSpeed", 0) or 0),
        progress=(done / total) if total else 0.0,
        error=err_msg,
        path=path,
    )


class DownloadManager:
    def __init__(
        self,
        *,
        download_dir: str,
        rpc_port: int,
        max_connections: int,
    ) -> None:
        self.download_dir = Path(download_dir).expanduser()
        self.rpc_port = rpc_port
        self.max_connections = max_connections
        self._secret = secrets.token_hex(16)
        self._proc: subprocess.Popen | None = None
        self._client: aria2p.Client | None = None
        self._session_file = self.download_dir / ".aria2-session"

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------- lifecycle --
    def start(self) -> None:
        """Spawn the aria2c daemon and connect. No-op (leaves `available` False) if the
        aria2c binary isn't installed, so the app still runs without downloads."""
        binary = shutil.which("aria2c")
        if binary is None:
            log.warning("aria2c not found on PATH — download manager disabled")
            return

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._session_file.touch(exist_ok=True)  # aria2 errors on a missing --input-file

        self._proc = subprocess.Popen(
            [
                binary,
                "--enable-rpc",
                "--rpc-listen-all=false",  # loopback only
                f"--rpc-listen-port={self.rpc_port}",
                f"--rpc-secret={self._secret}",
                "--continue=true",
                f"--dir={self.download_dir}",
                f"--save-session={self._session_file}",
                "--save-session-interval=30",
                f"--input-file={self._session_file}",
                f"--max-connection-per-server={self.max_connections}",
                f"--split={self.max_connections}",
                "--auto-file-renaming=false",
                "--allow-overwrite=true",
                "--console-log-level=warn",
                "--quiet=true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        client = aria2p.Client(host="http://127.0.0.1", port=self.rpc_port, secret=self._secret)
        # aria2c needs a moment to bind the RPC port; poll getVersion briefly.
        for _ in range(50):
            try:
                client.get_version()
                self._client = client
                log.info("aria2 download manager ready on 127.0.0.1:%d", self.rpc_port)
                return
            except Exception:
                if self._proc.poll() is not None:
                    log.error("aria2c exited during startup — download manager disabled")
                    return
                time.sleep(0.1)
        log.error("aria2c RPC did not come up in time — download manager disabled")

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.save_session()  # final flush for clean resume
                self._client.shutdown()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._client = None
        self._proc = None

    # --------------------------------------------------------------- actions --
    def _require(self) -> aria2p.Client:
        if self._client is None:
            raise RuntimeError("download manager unavailable")
        return self._client

    def enqueue(
        self,
        url: str,
        *,
        subdir: str = "",
        filename: str | None = None,
        headers: list[str] | None = None,
    ) -> str:
        """Queue a URL for download. `subdir` may be a nested relative path (each segment
        sanitized) under the base download dir; `filename` overrides the saved name;
        `headers` are extra request header lines (e.g. Authorization for raw fetches).
        Returns the aria2 GID."""
        options: dict[str, Any] = {}
        sub = _safe_subpath(subdir)
        if sub:
            options["dir"] = str(self.download_dir / sub)
        if filename:
            options["out"] = _safe_component(filename)
        if headers:
            options["header"] = headers
        return self._require().add_uri([url], options=options or None)

    def list(self) -> list[DownloadView]:
        client = self._require()
        rows = (
            client.tell_active(keys=_STATUS_KEYS)
            + client.tell_waiting(0, 1000, keys=_STATUS_KEYS)
            + client.tell_stopped(0, 1000, keys=_STATUS_KEYS)
        )
        return [_to_view(r) for r in rows]

    def views_by_gid(self) -> dict[str, DownloadView]:
        """Everything aria2 currently knows, keyed by GID — three RPC calls total, so
        it's cheap enough to run on every history reconcile/poll."""
        return {v.gid: v for v in self.list()}

    def free_bytes(self) -> int | None:
        """Free disk space at the download directory, or None if it can't be measured."""
        try:
            return shutil.disk_usage(self.download_dir).free
        except OSError:
            return None

    def pause(self, gid: str) -> None:
        self._require().pause(gid)

    def resume(self, gid: str) -> None:
        self._require().unpause(gid)

    def cancel(self, gid: str) -> None:
        """Remove an active/waiting download, or purge a finished/errored result row."""
        client = self._require()
        try:
            client.remove(gid)
        except Exception:
            client.remove_download_result(gid)


def _safe_component(name: str) -> str:
    """Reduce a name to a single safe path segment — no separators or parent refs — so a
    download can never be written outside the base download directory."""
    base = os.path.basename(name.replace("\\", "/").strip("/"))
    return base if base and base not in (".", "..") else "download"


def _safe_subpath(path: str) -> str:
    """Sanitize a nested relative path: drop empty/'.'/'..' segments so the joined path
    can never escape the base download directory."""
    parts = [
        p for p in path.replace("\\", "/").split("/") if p and p not in (".", "..")
    ]
    return "/".join(parts)
