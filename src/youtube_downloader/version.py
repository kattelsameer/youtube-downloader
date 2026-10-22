"""Application version, read from manifest.json at the repository root."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "manifest.json"


def read_manifest(path: Path = MANIFEST_PATH) -> dict:
    """Return manifest.json as a dict, or an empty dict if it is unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_version(path: Path = MANIFEST_PATH) -> str:
    return str(read_manifest(path).get("version") or "unknown")
