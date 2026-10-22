"""Run the real CLI with yt-dlp replaced by tests/fakes.py.

Used by tests that need a separate process (Ctrl+C, two instances at once):
    python tests/run_fake_cli.py --config ... [cli options]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src")]

import fakes  # noqa: E402
from youtube_downloader import backend, cli  # noqa: E402

backend.fetch_listing = fakes.fake_listing
backend.download_worker = fakes.fake_download

if __name__ == "__main__":
    sys.exit(cli.main(sys.argv[1:]))
