"""Detector tests — no network. Covers the pure version parse, base64 decode, the
registry, and the detector's run() against a fake contents client.
"""
from __future__ import annotations

import base64

import pytest

from packages.detectors import base as registry
from packages.detectors.unity import UnityVersionDetector, parse_unity_version

_PROJECT_VERSION = "m_EditorVersion: 2022.3.10f1\nm_EditorVersionWithRevision: 2022.3.10f1 (abc123)\n"


def test_parse_unity_version():
    assert parse_unity_version(_PROJECT_VERSION) == "2022.3.10f1"
    assert parse_unity_version("nothing here") is None
    assert parse_unity_version("m_EditorVersion: 6000.0.23f1") == "6000.0.23f1"


def test_unity_detector_registered():
    det = registry.get("unity_version")
    assert det is not None
    assert "unity_version" in registry.all_names()


class _FakeContentsGH:
    def __init__(self, text: str | None):
        self._text = text

    async def get_json(self, path: str, **_kw):
        if self._text is None:
            raise RuntimeError("404 not found")
        return {
            "encoding": "base64",
            "content": base64.b64encode(self._text.encode()).decode(),
        }


@pytest.mark.asyncio
async def test_unity_detector_run_success():
    det = UnityVersionDetector()
    fields = await det.run(_FakeContentsGH(_PROJECT_VERSION), owner="o", repo="r", repo_id=1)
    assert fields == {"unity_version": "2022.3.10f1"}


@pytest.mark.asyncio
async def test_unity_detector_run_absent_file():
    det = UnityVersionDetector()
    fields = await det.run(_FakeContentsGH(None), owner="o", repo="r", repo_id=1)
    assert fields == {}  # non-Unity repo → nothing recorded, no crash
