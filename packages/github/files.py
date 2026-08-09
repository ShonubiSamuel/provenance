"""Pure, network-free helpers for on-demand file/archive inspection.

Kept separate from the HTTP client so the fiddly bits — zip re-packaging and blob
decoding — are unit-testable without a GitHub token (see tests/test_inspect.py).
"""
from __future__ import annotations

import base64
import io
import zipfile
from typing import Any

# Files bigger than this aren't previewed inline (the client shows a download link
# instead). Matches the contents API's own ~1 MB inline ceiling.
MAX_TEXT_BYTES = 1_000_000

# On-the-fly folder zips fetch each file individually, so we cap them. Beyond this the
# UI steers the user to a whole-repo download or `git clone` instead. (A folder download
# must stay proportional to the FOLDER — never pull the entire repo, which is what made
# folder downloads hang on large repos.)
MAX_FOLDER_FILES = 400
MAX_FOLDER_ZIP_BYTES = 75 * 1024 * 1024


def zip_files(entries: list[tuple[str, bytes]]) -> bytes:
    """Zip up `(path, content)` pairs, preserving repo-relative paths. Pure/testable."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in entries:
            z.writestr(path, content)
    return buf.getvalue()


def folder_sizes(tree: list[dict[str, Any]]) -> dict[str, int]:
    """Sum a recursive git tree into a {folder_path: total_bytes} map.

    Each blob's size is added to EVERY ancestor folder, so a folder's total is the size
    of everything beneath it, at any depth. Keys are repo-relative folder paths with no
    trailing slash (e.g. `Assets/Scripts`) — exactly the `path` a contents-API directory
    entry carries, so the UI can look sizes up directly. Non-blob entries (trees,
    submodules, symlinks) contribute nothing themselves.
    """
    sizes: dict[str, int] = {}
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        size = entry.get("size") or 0
        parts = entry.get("path", "").split("/")
        for depth in range(1, len(parts)):  # every ancestor dir, excludes the file itself
            folder = "/".join(parts[:depth])
            sizes[folder] = sizes.get(folder, 0) + size
    return sizes


def decode_blob(node: dict[str, Any]) -> tuple[str, str | None]:
    """Turn a contents-API file object into `(encoding, text)`.

    `encoding` is one of: `text` (UTF-8 decodable, `text` populated), `binary` (bytes
    that aren't UTF-8), or `too_large` (GitHub didn't inline the content, or it exceeds
    MAX_TEXT_BYTES). The UI renders each case differently.
    """
    content = node.get("content")
    node_encoding = node.get("encoding")

    if node_encoding == "base64" and content:
        raw = base64.b64decode(content)
    elif isinstance(content, str) and content and node_encoding in (None, "utf-8"):
        raw = content.encode("utf-8")
    else:
        # Empty content → the contents API declined to inline it (file too big for it).
        return "too_large", None

    if len(raw) > MAX_TEXT_BYTES:
        return "too_large", None
    try:
        return "text", raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", None
