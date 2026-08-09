"""Repo-inspection tests — no network.

Pure helpers (zip filtering, blob decoding) are exercised directly; the endpoints are
driven through FastAPI's TestClient with the GitHub client dependency overridden by a
fake, so wiring + serialization are covered without a token.
"""
from __future__ import annotations

import base64
import io
import os
import zipfile
from contextlib import asynccontextmanager

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient

from packages.github.files import (
    MAX_TEXT_BYTES,
    decode_blob,
    folder_sizes,
    zip_files,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _make_zipball(paths: dict[str, bytes], top: str = "acme-game-abc123") -> bytes:
    """Build bytes shaped like a GitHub zipball: one top-level dir wrapping repo paths."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, content in paths.items():
            z.writestr(f"{top}/{rel}", content)
    return buf.getvalue()


def test_zip_files_preserves_paths():
    out = zip_files([("a/b.cs", b"hi"), ("a/c.txt", b"yo")])
    z = zipfile.ZipFile(io.BytesIO(out))
    assert set(z.namelist()) == {"a/b.cs", "a/c.txt"}
    assert z.read("a/b.cs") == b"hi"


def test_folder_sizes_accumulate_up_the_tree():
    tree = [
        {"path": "Assets", "type": "tree"},
        {"path": "Assets/Scripts", "type": "tree"},
        {"path": "Assets/Scripts/A.cs", "type": "blob", "size": 100},
        {"path": "Assets/Scripts/B.cs", "type": "blob", "size": 50},
        {"path": "Assets/art.png", "type": "blob", "size": 400},
        {"path": "README.md", "type": "blob", "size": 30},  # root file → no folder
        {"path": "sub", "type": "commit"},  # submodule → ignored
    ]
    sizes = folder_sizes(tree)
    assert sizes["Assets"] == 550  # 100 + 50 + 400
    assert sizes["Assets/Scripts"] == 150  # 100 + 50
    assert "README.md" not in sizes  # root-level file contributes to no folder
    assert "sub" not in sizes


def test_decode_blob_text():
    node = {"encoding": "base64", "content": base64.b64encode(b"hello").decode(), "size": 5}
    assert decode_blob(node) == ("text", "hello")


def test_decode_blob_binary():
    node = {"encoding": "base64", "content": base64.b64encode(b"\xff\xfe\x00").decode(), "size": 3}
    assert decode_blob(node) == ("binary", None)


def test_decode_blob_too_large():
    assert decode_blob({"encoding": "none", "content": "", "size": 5_000_000}) == ("too_large", None)
    big = base64.b64encode(b"x" * (MAX_TEXT_BYTES + 1)).decode()
    assert decode_blob({"encoding": "base64", "content": big, "size": MAX_TEXT_BYTES + 1}) == (
        "too_large", None,
    )


# --------------------------------------------------------------------------- #
# Endpoints (fake client injected via dependency override)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def aread(self) -> bytes:
        return self._data

    async def aiter_bytes(self):
        yield self._data

    def raise_for_status(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class FakeClient:
    def __init__(
        self, *, contents=None, file_bytes=b"", files=None, zip_bytes=b"", tree=None
    ) -> None:
        self._contents = contents or {}
        self._file = file_bytes
        self._files = files  # per-path bytes, for folder-zip tests
        self._zip = zip_bytes
        self._tree = tree or []

    async def get_contents(self, owner, repo, path="", ref=None):
        return self._contents[path]

    async def get_raw_file(self, owner, repo, path, ref=None):
        if self._files is not None:
            return self._files[path]
        return self._file

    async def get_recursive_tree(self, owner, repo, ref=None):
        return self._tree, False, "commitsha"

    @asynccontextmanager
    async def stream_zipball(self, owner, repo, ref=None):
        yield _FakeResp(self._zip)


@pytest.fixture
def client_with(monkeypatch):
    """TestClient factory that overrides the GitHub client dependency with a fake."""
    from apps.api.main import app, get_client

    def _make(fake: FakeClient) -> TestClient:
        app.dependency_overrides[get_client] = lambda: fake
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_contents_lists_dirs_first(client_with):
    fake = FakeClient(contents={"": [
        {"name": "z.cs", "path": "z.cs", "type": "file", "size": 1, "sha": "s1"},
        {"name": "Assets", "path": "Assets", "type": "dir", "size": None, "sha": "s2"},
        {"name": "a.cs", "path": "a.cs", "type": "file", "size": 2, "sha": "s3"},
    ]})
    with client_with(fake) as c:
        body = c.get("/repo/acme/game/contents").json()
    assert [(e["name"], e["type"]) for e in body["entries"]] == [
        ("Assets", "dir"), ("a.cs", "file"), ("z.cs", "file"),
    ]


def test_blob_returns_text(client_with):
    fake = FakeClient(contents={"README.md": {
        "encoding": "base64", "content": base64.b64encode(b"# Hi").decode(), "size": 4,
    }})
    with client_with(fake) as c:
        body = c.get("/repo/acme/game/blob", params={"path": "README.md"}).json()
    assert body["encoding"] == "text" and body["text"] == "# Hi"


def test_file_download_has_attachment_header(client_with):
    fake = FakeClient(file_bytes=b"print('hi')\n")
    with client_with(fake) as c:
        resp = c.get("/repo/acme/game/file", params={"path": "src/main.py"})
    assert resp.status_code == 200
    assert resp.content == b"print('hi')\n"
    assert 'filename="main.py"' in resp.headers["content-disposition"]


def test_archive_folder_zips_only_that_folder(client_with):
    # Folder download must be proportional to the FOLDER: enumerate its files from the
    # tree and zip only those — never pull the whole repo (which hangs on big repos).
    tree = [
        {"path": "Assets/A.cs", "type": "blob", "size": 3},
        {"path": "Assets/sub/B.cs", "type": "blob", "size": 3},
        {"path": "Other/C.cs", "type": "blob", "size": 3},
    ]
    files = {"Assets/A.cs": b"aaa", "Assets/sub/B.cs": b"bbb", "Other/C.cs": b"ccc"}
    fake = FakeClient(tree=tree, files=files)
    with client_with(fake) as c:
        resp = c.get("/repo/acme/game/archive", params={"path": "Assets"})
    assert resp.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(z.namelist()) == {"Assets/A.cs", "Assets/sub/B.cs"}  # "Other" excluded
    assert z.read("Assets/A.cs") == b"aaa"
    assert 'filename="game-Assets.zip"' in resp.headers["content-disposition"]


def test_archive_folder_too_large_returns_413(client_with):
    tree = [{"path": "Big/huge.bin", "type": "blob", "size": 200 * 1024 * 1024}]
    fake = FakeClient(tree=tree, files={"Big/huge.bin": b"x"})
    with client_with(fake) as c:
        resp = c.get("/repo/acme/game/archive", params={"path": "Big"})
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_archive_full_repo_streams(client_with):
    zip_bytes = _make_zipball({"README.md": b"hi"})
    fake = FakeClient(zip_bytes=zip_bytes)
    with client_with(fake) as c:
        resp = c.get("/repo/acme/game/archive")
    assert resp.status_code == 200
    assert resp.content == zip_bytes  # streamed through untouched
    assert 'filename="game.zip"' in resp.headers["content-disposition"]


def test_sizes_endpoint_returns_folder_totals(client_with):
    fake = FakeClient(tree=[
        {"path": "Assets/Scripts/A.cs", "type": "blob", "size": 100},
        {"path": "Assets/art.png", "type": "blob", "size": 400},
    ])
    with client_with(fake) as c:
        body = c.get("/repo/acme/game/sizes").json()
    assert body["sizes"]["Assets"] == 500
    assert body["sizes"]["Assets/Scripts"] == 100
    assert body["truncated"] is False


def test_guard_rejects_path_traversal(client_with):
    with client_with(FakeClient()) as c:
        assert c.get("/repo/acme/..%2fetc/contents").status_code in (400, 404)
