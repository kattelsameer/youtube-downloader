#!/usr/bin/env python3
"""Download audio or video from YouTube, driven by a multi-job YAML config.

Every job syncs a single video or a playlist into ``<output_dir>/<tag>/``.
A shared JSON metadata file records what has been downloaded, so later runs
only fetch videos that are new (incremental sync).

Usage: python downloader.py --config config.yaml [--job TAG] [--dry-run] ...
The code lives in src/youtube_downloader/; this file is the entry point.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from youtube_downloader.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
