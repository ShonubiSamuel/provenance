"""Detector plugin contract.

A detector inspects a repo (usually by fetching one or two specific files) and writes
derived facts onto the Repository row or a side table. This is the primary extensibility
seam: Unity version, vendored-vs-package, Unreal, React, asset fingerprinting all
implement this same interface without touching the discovery/enrichment stages.
"""
from __future__ import annotations

from typing import Any, Protocol


class Detector(Protocol):
    name: str

    async def run(self, gh: Any, *, owner: str, repo: str, repo_id: int) -> dict[str, Any]:
        """Return a dict of fields to persist (e.g. {'unity_version': '2022.3.10f1'}).
        Return {} when nothing is detected. Must be side-effect-free w.r.t. the DB —
        the stage runner persists the returned fields.
        """
        ...


_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    _REGISTRY[detector.name] = detector
    return detector


def get(name: str) -> Detector | None:
    return _REGISTRY.get(name)


def all_names() -> list[str]:
    return list(_REGISTRY)
