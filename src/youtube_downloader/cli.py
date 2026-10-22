"""Command-line interface: python downloader.py --config config.yaml [options]."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import FORMATS, Config, ConfigError, load_config, select_jobs
from .logs import configure_logging
from .metadata import STATUS_DOWNLOADED, STATUS_FAILED, LockError, MetadataError, MetadataLock, MetadataStore
from .runner import RunOptions, emit, run_jobs
from .version import read_manifest, read_version

log = logging.getLogger("ytdl")

EXIT_OK = 0
EXIT_FAILURES = 1        # some videos or jobs failed
EXIT_USAGE = 2           # bad config or arguments
EXIT_LOCKED = 3          # another instance holds the lock
EXIT_METADATA = 4        # metadata file can't be used safely
EXIT_INTERRUPTED = 130   # Ctrl+C

EXAMPLES = """\
examples:
  python downloader.py --config config.yaml              run all enabled jobs
  python downloader.py --job song-playlist --dry-run     preview one job
  python downloader.py --list                            jobs and tracked videos
  python downloader.py --status --job song-playlist      metadata report
  python downloader.py --retry-failed                    retry earlier failures
  python downloader.py --format mp4 --max-workers 2      override settings

exit codes: 0 ok, 1 some downloads failed, 2 config error, 3 already running,
4 metadata unusable, 130 interrupted
"""


def build_arg_parser() -> argparse.ArgumentParser:
    manifest = read_manifest()
    parser = argparse.ArgumentParser(
        prog="downloader.py",
        description="Download YouTube audio/video from a multi-job YAML config, "
                    "only fetching what is new since the last run.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", metavar="PATH",
                        help="YAML config file (default: config.yaml)")
    parser.add_argument("--job", action="append", metavar="TAG",
                        help="only this job; repeat for several")
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--list", action="store_true",
                         help="show the jobs and how many videos each one tracks; no downloading")
    command.add_argument("--dry-run", action="store_true",
                         help="show what would be downloaded or skipped; changes nothing")
    command.add_argument("--status", action="store_true",
                         help="report what the metadata file records for each job")
    parser.add_argument("--retry-failed", action="store_true",
                        help="retry videos that failed on an earlier run")
    parser.add_argument("--force", action="store_true",
                        help="ignore the metadata and re-download every selected video")
    parser.add_argument("--format", choices=FORMATS, help="override the format of every job")
    parser.add_argument("--output-dir", metavar="DIR", help="override output_dir for every job")
    parser.add_argument("--max-workers", type=int, metavar="N",
                        help="override max_workers for every job")
    parser.add_argument("--version", action="version",
                        version=f"{manifest.get('name', 'youtube-downloader')} {read_version()}")
    return parser


def display_path(path: Path) -> str:
    """Path relative to the current folder when it is inside it."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _video_counts(entry: dict | None, fmt: str | None = None) -> dict:
    counts = {"tracked": 0, "downloaded": 0, "failed": 0, "formats": {}}
    for formats in (entry or {}).get("videos", {}).values():
        counts["tracked"] += 1
        for name, record in formats.items():
            per = counts["formats"].setdefault(name, {STATUS_DOWNLOADED: 0, STATUS_FAILED: 0})
            status = record.get("status")
            if status in per:
                per[status] += 1
            if fmt is None or name == fmt:
                counts["downloaded"] += status == STATUS_DOWNLOADED
                counts["failed"] += status == STATUS_FAILED
    return counts


def cmd_list(config: Config, store: MetadataStore) -> int:
    emit(f"Config:   {display_path(config.path)}")
    emit(f"Metadata: {display_path(config.metadata_file)}")
    rows = [("TAG", "MODE", "FORMAT", "ENABLED", "TRACKED", "DOWNLOADED", "URL")]
    for job in config.jobs:
        counts = _video_counts(store.job_entry(job.tag), job.format)
        rows.append((job.name, job.mode, job.format, "yes" if job.enabled else "no",
                     str(counts["tracked"]), f"{counts['downloaded']} {job.format}", job.url))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        emit("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1])
    return EXIT_OK


def cmd_status(config: Config, store: MetadataStore, tags: list[str] | None) -> int:
    data = store.data
    emit(f"Metadata: {display_path(config.metadata_file)} (schema {data.get('schema_version')}, "
         f"updated {data.get('updated_at') or 'never'})")
    entries = data["jobs"]
    if tags:
        wanted = set()
        for tag in tags:
            job = config.find_job(tag)
            key = job.tag if job else tag
            if key not in entries and job is None:
                log.error("--job %r: not in the config or the metadata", tag)
                return EXIT_USAGE
            wanted.add(key)
        entries = {k: v for k, v in entries.items() if k in wanted}
    if not entries:
        emit("No downloads recorded yet.")
        return EXIT_OK
    config_tags = {job.tag for job in config.jobs}
    for tag, entry in entries.items():
        where = "" if tag in config_tags else "  (not in config any more; kept untouched)"
        emit(f"\n{tag}{where}")
        emit(f"  url:       {entry.get('url')}")
        if entry.get("playlist_title"):
            emit(f"  playlist:  {entry['playlist_title']} ({entry.get('playlist_id')})")
        emit(f"  last run:  {entry.get('last_run') or 'never'}")
        counts = _video_counts(entry)
        emit(f"  videos:    {counts['tracked']} tracked")
        for fmt, per in sorted(counts["formats"].items()):
            emit(f"  {fmt}:       {per[STATUS_DOWNLOADED]} downloaded, {per[STATUS_FAILED]} failed")
        failed = [(vid, fmt, rec) for vid, formats in entry.get("videos", {}).items()
                  for fmt, rec in formats.items() if rec.get("status") == STATUS_FAILED]
        if failed:
            emit("  failed videos:")
            for vid, fmt, rec in failed:
                emit(f"    - {rec.get('title') or '(unknown title)'} [{vid}] {fmt}, "
                     f"{rec.get('attempts', 0)} attempt(s): {rec.get('last_error')}")
    return EXIT_OK


def cmd_run(config: Config, jobs, args) -> int:
    options = RunOptions(retry_failed=args.retry_failed, force=args.force, dry_run=args.dry_run)
    emit(f"{read_manifest().get('name', 'youtube-downloader')} {read_version()} | "
         f"config {display_path(config.path)} | metadata {display_path(config.metadata_file)}")
    if args.dry_run:
        store = MetadataStore(config.metadata_file, read_only=True).load()
        summaries = run_jobs(jobs, store, options)
    else:
        with MetadataLock(config.metadata_file):
            store = MetadataStore(config.metadata_file).load()
            summaries = run_jobs(jobs, store, options)
    if any(s.interrupted for s in summaries):
        return EXIT_INTERRUPTED
    if any(s.error or s.failed for s in summaries):
        return EXIT_FAILURES
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configure_logging("INFO")
    overrides = {"format": args.format, "output_dir": args.output_dir, "max_workers": args.max_workers}
    try:
        config = load_config(args.config, overrides=overrides)
        jobs = select_jobs(config, None if args.status else args.job)
    except ConfigError as exc:
        print(f"Config error in {args.config}:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_USAGE
    configure_logging(config.defaults["log_level"])
    for warning in config.warnings:
        log.warning("%s", warning)

    try:
        if args.list or args.status:
            store = MetadataStore(config.metadata_file, read_only=True).load()
            return cmd_list(config, store) if args.list else cmd_status(config, store, args.job)
        return cmd_run(config, jobs, args)
    except LockError as exc:
        log.error("%s", exc)
        return EXIT_LOCKED
    except MetadataError as exc:
        log.error("%s", exc)
        return EXIT_METADATA
    except KeyboardInterrupt:
        emit("\nInterrupted.")
        return EXIT_INTERRUPTED
