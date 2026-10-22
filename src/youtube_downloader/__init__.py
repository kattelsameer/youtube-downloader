"""Config-driven YouTube downloader with incremental playlist sync.

Modules:
    version   read the app version from manifest.json
    config    load, validate and merge config.yaml
    metadata  the shared metadata file (atomic writes, lock file, recovery)
    sync      decide what each job needs to download
    cli       command-line entry point (run via ../../downloader.py)
"""

from .version import read_version

__version__ = read_version()
