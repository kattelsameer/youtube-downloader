#!/usr/bin/env python3
"""Download audio or video from YouTube, driven by a multi-job YAML config.

Every job syncs a single video or a playlist into ``<output_dir>/<tag>/``.
A shared JSON metadata file records what has been downloaded, so later runs
only fetch videos that are new (incremental sync).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = APP_DIR / "manifest.json"


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    manifest = read_manifest()
    parser = argparse.ArgumentParser(
        prog="downloader.py",
        description="Download YouTube audio/video from a multi-job YAML config, "
                    "only fetching what is new since the last run.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"{manifest.get('name', 'youtube-downloader')} {read_version()}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
