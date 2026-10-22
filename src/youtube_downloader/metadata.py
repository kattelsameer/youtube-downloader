"""The shared metadata file: atomic writes, lock file, corrupt-file recovery."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .version import read_version

if TYPE_CHECKING:
    from .config import Job

log = logging.getLogger("ytdl")


SCHEMA_VERSION = 1
STATUS_DOWNLOADED = "downloaded"
STATUS_FAILED = "failed"

if os.name == "nt":
    import msvcrt

    def _lock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class MetadataError(Exception):
    """The metadata file cannot be used safely (e.g. written by a newer version)."""


class LockError(Exception):
    """Another instance of the tool holds the metadata lock."""

    def __init__(self, message: str, path: Path):
        super().__init__(message)
        self.path = path


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data) -> None:
    """Write JSON to a temp file in the same folder, fsync it, then rename it over `path`.

    Readers only ever see the old or the new file, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class MetadataLock:
    """Exclusive, non-blocking lock on `<metadata_file>.lock`, held for a whole run.

    The OS drops the lock if the process dies, so a crash never leaves a stale
    lock. The file itself stays in place: deleting it would race with another
    instance that is just opening it.
    """

    def __init__(self, metadata_path: Path):
        self.path = Path(metadata_path).with_name(Path(metadata_path).name + ".lock")
        self._fh = None

    def acquire(self) -> MetadataLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            _lock_file(fh)
        except OSError:
            holder = ""
            with contextlib.suppress(OSError):
                fh.seek(0)
                holder = fh.read().strip()
            fh.close()
            detail = f" ({holder})" if holder else ""
            raise LockError(f"another instance is already running{detail}", self.path) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since {utc_now()}\n")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            self._fh.seek(0)
            self._fh.truncate()
            _unlock_file(self._fh)
        self._fh.close()
        self._fh = None

    def __enter__(self) -> MetadataLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _check_metadata_structure(data) -> None:
    """Raise MetadataError for a newer schema, ValueError for anything malformed."""
    if not isinstance(data, dict) or not isinstance(data.get("schema_version"), int):
        raise ValueError("missing schema_version")
    if data["schema_version"] > SCHEMA_VERSION:
        raise MetadataError(
            f"metadata schema_version {data['schema_version']} is newer than this tool supports "
            f"({SCHEMA_VERSION}); update the tool instead of overwriting the file")
    if not isinstance(data.get("jobs"), dict):
        raise ValueError("'jobs' must be an object")
    for tag, entry in data["jobs"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("videos", {}), dict):
            raise ValueError(f"job {tag!r} is malformed")
        for video_id, formats in entry.get("videos", {}).items():
            if not isinstance(formats, dict) or not all(isinstance(r, dict) for r in formats.values()):
                raise ValueError(f"video {video_id!r} in job {tag!r} is malformed")
    if not isinstance(data.get("upload_dates", {}), dict):
        raise ValueError("'upload_dates' must be an object")


class MetadataStore:
    """The single source of truth for what each job has downloaded.

    Layout: jobs[tag].videos[video_id][format] = record. Keeping one record
    per format means switching a job from mp3 to mp4 downloads everything
    again without losing the mp3 records. Only the main process touches it;
    workers send results back and the main process records them here.
    """

    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.data = self._empty()
        self.recovered_from: Path | None = None

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": SCHEMA_VERSION, "app_version": read_version(),
                "updated_at": None, "jobs": {}, "upload_dates": {}}

    def load(self) -> MetadataStore:
        if not self.path.exists():
            return self
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            _check_metadata_structure(data)
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            self._recover(exc)
            return self
        data.setdefault("upload_dates", {})
        for entry in data["jobs"].values():
            entry.setdefault("videos", {})
        self.data = data
        return self

    def _recover(self, exc: Exception) -> None:
        if self.read_only:
            log.warning("Metadata file %s is corrupt or unreadable (%s); treating it as "
                        "empty. A normal run will back it up and start fresh.", self.path, exc)
            return
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.bak.{stamp}")
        suffix = 1
        while backup.exists():
            backup = self.path.with_name(f"{self.path.name}.bak.{stamp}-{suffix}")
            suffix += 1
        try:
            os.replace(self.path, backup)
        except OSError as move_exc:
            raise MetadataError(f"metadata file {self.path} is corrupt ({exc}) and could not be "
                                f"backed up: {move_exc}") from None
        self.recovered_from = backup
        log.warning("Metadata file %s is corrupt or unreadable (%s). Backed it up to %s "
                    "and starting with empty metadata.", self.path, exc, backup)

    def save(self) -> None:
        if self.read_only:
            raise RuntimeError("metadata store is read-only")
        self.data["updated_at"] = utc_now()
        self.data["app_version"] = read_version()
        atomic_write_json(self.path, self.data)

    # --- queries -----------------------------------------------------------

    @property
    def upload_dates(self) -> dict:
        return self.data["upload_dates"]

    def job_entry(self, tag: str) -> dict | None:
        return self.data["jobs"].get(tag)

    def video_record(self, tag: str, video_id: str, fmt: str) -> dict | None:
        entry = self.job_entry(tag) or {}
        return entry.get("videos", {}).get(video_id, {}).get(fmt)

    # --- updates (main process only) ---------------------------------------

    def ensure_job(self, job: Job) -> dict:
        entry = self.data["jobs"].setdefault(job.tag, {
            "url": job.url, "mode": job.mode, "format": job.format,
            "playlist_id": None, "playlist_title": None, "last_run": None, "videos": {},
        })
        entry.update(url=job.url, mode=job.mode, format=job.format)
        return entry

    def record_result(self, job: Job, result: dict) -> dict:
        """Store one worker result for `job` and flush the file right away."""
        fmt = job.format
        formats = self.ensure_job(job)["videos"].setdefault(result["video_id"], {})
        previous = formats.get(fmt) or {}
        record = {
            "title": result.get("title") or previous.get("title"),
            "uploader": result.get("uploader") or previous.get("uploader"),
            "duration": result.get("duration") or previous.get("duration"),
            "upload_date": result.get("upload_date") or previous.get("upload_date"),
            "format": fmt,
            "file_path": previous.get("file_path"),
            "file_size": previous.get("file_size"),
            "status": STATUS_FAILED,
            "downloaded_at": previous.get("downloaded_at"),
            "attempts": int(previous.get("attempts") or 0) + 1,
            "last_error": None,
        }
        if result["ok"]:
            record.update(file_path=result["file_path"], file_size=result.get("file_size"),
                          status=STATUS_DOWNLOADED, downloaded_at=utc_now())
        else:
            record["last_error"] = result.get("error") or "unknown error"
            # A failed forced re-download must not hide a good file that is still there.
            if (previous.get("status") == STATUS_DOWNLOADED and previous.get("file_path")
                    and (job.output_dir / previous["file_path"]).exists()):
                record["status"] = STATUS_DOWNLOADED
        formats[fmt] = record
        if record["upload_date"]:
            self.upload_dates[result["video_id"]] = record["upload_date"]
        self.save()
        return record

    def finish_job(self, job: Job, playlist_id: str | None = None,
                   playlist_title: str | None = None) -> None:
        entry = self.ensure_job(job)
        if playlist_id or playlist_title:
            entry.update(playlist_id=playlist_id, playlist_title=playlist_title)
        entry["last_run"] = utc_now()
        self.save()
