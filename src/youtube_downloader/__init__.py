"""Config-driven YouTube downloader with incremental playlist sync.

Modules:
    version   read the app version from manifest.json
    config    load, validate and merge config.yaml
    metadata  the shared metadata file (atomic writes, lock file, recovery)
    sync      decide what each job needs to download
    backend   yt-dlp listing and the download worker
    runner    run jobs: process pool, recording results, summaries
    logs      logging setup for the main process and workers
    cli       command-line entry point (run via ../../downloader.py)
"""

from .version import read_version

__version__ = read_version()
