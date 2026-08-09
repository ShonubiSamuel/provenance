"""Detector plugins. Importing this package registers the built-in detectors so the
registry (`base.get`) is populated wherever detectors are used.
"""
from packages.detectors import unity  # noqa: F401 — import side effect: registration

__all__ = ["unity"]
