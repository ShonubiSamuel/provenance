"""Unity version detector.

Reads `ProjectSettings/ProjectVersion.txt` (one CORE contents fetch) and parses the
`m_EditorVersion:` line, e.g. `2022.3.10f1`. This is what lets the UI filter results by
Unity version — something GitHub can't surface at all.
"""
from __future__ import annotations

import base64
import re
from typing import Any

from packages.detectors.base import register

_EDITOR_VERSION = re.compile(r"^m_EditorVersion:\s*(?P<v>\S+)", re.MULTILINE)


def parse_unity_version(text: str) -> str | None:
    """Extract the editor version from ProjectVersion.txt contents. Pure + testable."""
    m = _EDITOR_VERSION.search(text)
    return m.group("v").strip() if m else None


def _decode_contents(data: Any) -> str | None:
    """The contents API returns base64 for files. Return decoded text or None."""
    if not isinstance(data, dict):
        return None
    if data.get("encoding") == "base64" and data.get("content"):
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
    # Some responses inline small files as raw text.
    content = data.get("content")
    return content if isinstance(content, str) else None


class UnityVersionDetector:
    name = "unity_version"

    async def run(self, gh: Any, *, owner: str, repo: str, repo_id: int) -> dict[str, Any]:
        try:
            data = await gh.get_json(
                f"/repos/{owner}/{repo}/contents/ProjectSettings/ProjectVersion.txt"
            )
        except Exception:
            return {}  # not a Unity project, or file absent — nothing to record
        text = _decode_contents(data)
        if not text:
            return {}
        version = parse_unity_version(text)
        return {"unity_version": version} if version else {}


register(UnityVersionDetector())
