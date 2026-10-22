#!/usr/bin/env python3
"""Download audio or video from YouTube, driven by a multi-job YAML config.

Every job syncs a single video or a playlist into ``<output_dir>/<tag>/``.
A shared JSON metadata file records what has been downloaded, so later runs
only fetch videos that are new (incremental sync).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import json
import logging
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

APP_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = APP_DIR / "manifest.json"

log = logging.getLogger("ytdl")


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
# Config: loading, validation and merging (CLI > job > defaults > built-in)
# ---------------------------------------------------------------------------

FORMATS = ("mp3", "mp4")
MODES = ("single", "playlist")
ORDERS = ("playlist", "newest", "oldest", "title_asc", "title_desc", "shortest", "longest")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
JS_RUNTIMES = ("auto", "deno", "node", "bun", "quickjs", "none")

# Every setting a job inherits. `defaults:` in the config overrides these.
BUILTIN_DEFAULTS: dict = {
    "format": "mp3",
    "audio_quality": 192,
    "max_resolution": 720,
    "output_dir": "downloads",
    "filename_template": "%(title)s [%(id)s].%(ext)s",
    "use_playlist_subfolder": False,
    "order": "playlist",
    "mix_limit": 25,
    "max_workers": 3,
    "concurrent_fragment_downloads": 4,
    "retries": 3,
    "socket_timeout": 30,
    "rate_limit": None,
    "skip_existing": True,
    "verify_files": True,
    "retry_failed": False,
    "cookies_file": None,
    "proxy": None,
    "embed_metadata": True,
    "embed_thumbnail": False,
    "js_runtime": "auto",
    "log_level": "INFO",
}
GLOBAL_ONLY_KEYS = frozenset({"metadata_file"})
JOB_ONLY_KEYS = frozenset({"tag", "enabled", "mode", "url", "count"})
REQUIRED_JOB_KEYS = ("tag", "mode", "url")
CLI_OVERRIDE_KEYS = ("format", "output_dir", "max_workers")

YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
})
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_PATH_PREFIXES = ("/@", "/channel/", "/c/", "/user/")
WINDOWS_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)]
)


class ConfigError(Exception):
    """One or more problems in the config; `errors` holds one message each."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class Job:
    tag: str            # sanitized tag: folder name and metadata key
    name: str           # tag exactly as written in the config
    mode: str
    url: str
    enabled: bool
    count: int | None   # None means "all"
    settings: dict      # merged settings, paths already resolved

    @property
    def format(self) -> str:
        return self.settings["format"]

    @property
    def output_dir(self) -> Path:
        return Path(self.settings["output_dir"])

    @property
    def tag_dir(self) -> Path:
        return self.output_dir / self.tag


@dataclass
class Config:
    path: Path
    defaults: dict
    metadata_file: Path
    jobs: list[Job]
    warnings: list[str] = field(default_factory=list)

    def find_job(self, tag: str) -> Job | None:
        wanted = tag.casefold()
        for job in self.jobs:
            if wanted in (job.tag.casefold(), job.name.casefold()):
                return job
        return None


# --- value validators: return the normalized value or raise ValueError -----

def _v_bool(value):
    if isinstance(value, bool):
        return value
    raise ValueError("must be true or false")


def _v_int(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"must be an integer from {lo} to {hi}")
        return value
    return check


def _v_choice(choices, upper=False):
    def check(value):
        text = str(value).strip()
        text = text.upper() if upper else text.lower()
        if isinstance(value, bool) or text not in choices:
            raise ValueError(f"must be one of {', '.join(choices)}")
        return text
    return check


def _v_str(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value.strip()


def _v_optional_str(value):
    return None if value in (None, "") else _v_str(value)


def _v_audio_quality(value):
    text = str(value).strip().lower().removesuffix("k")
    if isinstance(value, bool) or not text.isdigit() or not 32 <= int(text) <= 320:
        raise ValueError("must be a bitrate in kbps from 32 to 320 (e.g. 192 or 320)")
    return int(text)


def _v_max_resolution(value):
    text = str(value).strip().lower().removesuffix("p")
    if text == "best":
        return "best"
    if isinstance(value, bool) or not text.isdigit() or not 144 <= int(text) <= 4320:
        raise ValueError("must be 'best' or a height from 144 to 4320 (e.g. 360, 720, 1080)")
    return int(text)


def _v_rate_limit(value):
    """Bytes per second; accepts 500000, '500K', '2M', '1.5MiB'."""
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?\s*", str(value), re.IGNORECASE)
    if isinstance(value, bool) or not match or float(match.group(1)) <= 0:
        raise ValueError("must be bytes per second, e.g. 500K or 2M")
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def _v_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("must be a positive number of seconds")
    return value


def _v_template(value):
    text = _v_str(value)
    if "%(ext)s" not in text:
        raise ValueError("must contain %(ext)s, e.g. '%(title)s [%(id)s].%(ext)s'")
    if Path(text).is_absolute() or ".." in Path(text).parts:
        raise ValueError("must be a relative file name inside the job folder")
    return text


SETTING_VALIDATORS = {
    "format": _v_choice(FORMATS),
    "audio_quality": _v_audio_quality,
    "max_resolution": _v_max_resolution,
    "output_dir": _v_str,
    "filename_template": _v_template,
    "use_playlist_subfolder": _v_bool,
    "order": _v_choice(ORDERS),
    "mix_limit": _v_int(1, 1000),
    "max_workers": _v_int(1, 32),
    "concurrent_fragment_downloads": _v_int(1, 64),
    "retries": _v_int(0, 100),
    "socket_timeout": _v_timeout,
    "rate_limit": _v_rate_limit,
    "skip_existing": _v_bool,
    "verify_files": _v_bool,
    "retry_failed": _v_bool,
    "cookies_file": _v_optional_str,
    "proxy": _v_optional_str,
    "embed_metadata": _v_bool,
    "embed_thumbnail": _v_bool,
    "js_runtime": _v_choice(JS_RUNTIMES),
    "log_level": _v_choice(LOG_LEVELS, upper=True),
}
assert set(SETTING_VALIDATORS) == set(BUILTIN_DEFAULTS)


def sanitize_tag(raw) -> str:
    """Turn a job tag into a safe, portable folder name ('' if nothing is left)."""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w.-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip(".-_")
    return text[:64].rstrip(".-_")


def sanitize_folder_name(raw, max_len: int = 100) -> str:
    """Make a playlist title usable as a folder name (spaces kept)."""
    text = unicodedata.normalize("NFKC", str(raw))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:max_len].rstrip(" .") or "playlist"


def parse_youtube_url(url: str) -> tuple[str | None, str | None]:
    """Return (video_id, playlist_id) from a YouTube URL; ValueError if not YouTube."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        raise ValueError("is not a YouTube URL (expected https://www.youtube.com/... or https://youtu.be/...)")
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [None])[0]
    parts = [p for p in parsed.path.split("/") if p]
    video_id = None
    if host == "youtu.be":
        video_id = parts[0] if parts else None
    elif parsed.path == "/watch":
        video_id = (query.get("v") or [None])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        video_id = parts[1]
    if video_id is not None and not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(f"has an invalid video id {video_id!r}")
    return video_id, playlist_id


def is_collection_url(url: str) -> bool:
    """True for playlist and channel URLs (things that list several videos)."""
    _, playlist_id = parse_youtube_url(url)
    path = urlparse(url.strip()).path
    return bool(playlist_id) or path == "/playlist" or path.startswith(CHANNEL_PATH_PREFIXES)


def is_mix_playlist(playlist_id: str | None) -> bool:
    """Auto-generated Mix/radio playlists change on every fetch and never end."""
    return bool(playlist_id) and playlist_id.startswith("RD")


def _check_keys(section: dict, allowed, label: str, errors: list, hints: dict | None = None):
    for key in section:
        if key in allowed:
            continue
        if hints and key in hints:
            errors.append(f"{label}: {key!r} {hints[key]}")
            continue
        close = difflib.get_close_matches(str(key), list(allowed), n=1)
        suggestion = f" (did you mean {close[0]!r}?)" if close else ""
        errors.append(f"{label}: unknown key {key!r}{suggestion}")


def _validate_settings(section: dict, label: str, errors: list) -> dict:
    clean = {}
    for key, validator in SETTING_VALIDATORS.items():
        if key in section:
            try:
                clean[key] = validator(section[key])
            except ValueError as exc:
                errors.append(f"{label}: {key}: {exc} (got {section[key]!r})")
    return clean


def _resolve_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def _parse_count(value):
    if isinstance(value, str) and value.strip().lower() == "all":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'must be a positive integer or "all" (got {value!r})')
    return value


def load_config(path, overrides: dict | None = None, cwd: Path | None = None) -> Config:
    """Load and validate the YAML config. Raises ConfigError listing every problem.

    `overrides` holds CLI values (format, output_dir, max_workers); a relative
    CLI output_dir is resolved against `cwd`, config paths against the config's folder.
    """
    path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from None

    errors: list[str] = []
    warnings: list[str] = []
    base = path.resolve().parent
    cwd = Path(cwd or Path.cwd())

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping with 'defaults' and 'jobs'")
    _check_keys(raw, ("defaults", "jobs"), "config", errors)

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        errors.append("defaults: must be a mapping of settings")
        defaults_raw = {}
    _check_keys(defaults_raw, set(BUILTIN_DEFAULTS) | GLOBAL_ONLY_KEYS, "defaults", errors,
                hints={k: "is a job setting; set it on each job" for k in JOB_ONLY_KEYS})
    defaults = {**BUILTIN_DEFAULTS, **_validate_settings(defaults_raw, "defaults", errors)}

    cli = {k: v for k, v in (overrides or {}).items() if v is not None}
    _check_keys(cli, CLI_OVERRIDE_KEYS, "command line", errors)
    cli = _validate_settings(cli, "command line", errors)
    if "output_dir" in cli:
        cli["output_dir"] = _resolve_path(cli["output_dir"], cwd)

    jobs_raw = raw.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        errors.append("jobs: must be a non-empty list of jobs")
        jobs_raw = []

    jobs: list[Job] = []
    seen_tags: dict[str, str] = {}
    for index, job_raw in enumerate(jobs_raw):
        label = f"jobs[{index}]"
        if not isinstance(job_raw, dict):
            errors.append(f"{label}: must be a mapping of job settings")
            continue
        if isinstance(job_raw.get("tag"), (str, int)) and not isinstance(job_raw.get("tag"), bool):
            label = f"jobs[{index}] (tag {str(job_raw['tag'])!r})"
        before = len(errors)
        _check_keys(job_raw, set(BUILTIN_DEFAULTS) | JOB_ONLY_KEYS, label, errors,
                    hints={"metadata_file": "is global only; set it under defaults"})
        for key in REQUIRED_JOB_KEYS:
            if job_raw.get(key) in (None, ""):
                errors.append(f"{label}: missing required key {key!r}")

        tag = ""
        raw_tag = job_raw.get("tag")
        if raw_tag not in (None, ""):
            if isinstance(raw_tag, bool) or not isinstance(raw_tag, (str, int)):
                errors.append(f"{label}: tag must be a short text label")
            else:
                tag = sanitize_tag(raw_tag)
                if not tag:
                    errors.append(f"{label}: tag {raw_tag!r} has no usable characters for a folder name")
                elif tag.casefold() in WINDOWS_RESERVED_NAMES:
                    errors.append(f"{label}: tag {raw_tag!r} is a reserved file name")
                elif tag.casefold() in seen_tags:
                    errors.append(f"{label}: duplicate tag {raw_tag!r} (same folder {tag!r} as "
                                  f"{seen_tags[tag.casefold()]})")
                else:
                    seen_tags[tag.casefold()] = label

        mode = None
        if job_raw.get("mode") not in (None, ""):
            try:
                mode = _v_choice(MODES)(job_raw["mode"])
            except ValueError as exc:
                errors.append(f"{label}: mode: {exc} (got {job_raw['mode']!r})")

        url = None
        if job_raw.get("url") not in (None, ""):
            try:
                url = _v_str(job_raw["url"])
                video_id, _ = parse_youtube_url(url)
                if mode == "single" and not video_id:
                    errors.append(f"{label}: url: single mode needs a video URL "
                                  f"(watch?v=..., youtu.be/..., /shorts/...), got {url!r}")
                elif mode == "playlist" and not is_collection_url(url):
                    errors.append(f"{label}: url: playlist mode needs a playlist URL with list=... "
                                  f"or a channel URL, got {url!r}")
            except ValueError as exc:
                errors.append(f"{label}: url: {exc} (got {job_raw['url']!r})")

        enabled = True
        if "enabled" in job_raw:
            try:
                enabled = _v_bool(job_raw["enabled"])
            except ValueError as exc:
                errors.append(f"{label}: enabled: {exc} (got {job_raw['enabled']!r})")

        count = None
        if "count" in job_raw:
            try:
                count = _parse_count(job_raw["count"])
            except ValueError as exc:
                errors.append(f"{label}: count: {exc}")
            if mode == "single":
                warnings.append(f"{label}: count is ignored in single mode")

        settings = {**defaults, **_validate_settings(job_raw, label, errors), **cli}
        if len(errors) > before:
            continue
        for key in ("output_dir", "cookies_file"):
            if settings[key] and key not in cli:
                settings[key] = _resolve_path(settings[key], base)
        if settings["cookies_file"] and not Path(settings["cookies_file"]).is_file():
            errors.append(f"{label}: cookies_file not found: {settings['cookies_file']}")
            continue
        jobs.append(Job(tag=tag, name=str(raw_tag), mode=mode, url=url, enabled=enabled,
                        count=count, settings=settings))

    if errors:
        raise ConfigError(errors)

    merged_defaults = {**defaults, **cli}
    if "output_dir" not in cli:
        merged_defaults["output_dir"] = _resolve_path(merged_defaults["output_dir"], base)
    metadata_file = defaults_raw.get("metadata_file")
    if metadata_file not in (None, ""):
        if not isinstance(metadata_file, str):
            raise ConfigError(f"defaults: metadata_file: must be a file path (got {metadata_file!r})")
        metadata_path = Path(_resolve_path(metadata_file, base))
    else:
        metadata_path = Path(merged_defaults["output_dir"]) / "metadata.json"
    return Config(path=path.resolve(), defaults=merged_defaults, metadata_file=metadata_path,
                  jobs=jobs, warnings=warnings)


def select_jobs(config: Config, tags: list[str] | None) -> list[Job]:
    """Jobs named by --job (in config order), or all jobs. Unknown tags are an error."""
    if not tags:
        return list(config.jobs)
    unknown = [t for t in tags if config.find_job(t) is None]
    if unknown:
        available = ", ".join(j.name for j in config.jobs)
        raise ConfigError([f"--job {t!r}: no such job (available: {available})" for t in unknown])
    wanted = {config.find_job(t).tag for t in tags}
    return [job for job in config.jobs if job.tag in wanted]


# ---------------------------------------------------------------------------
# Metadata store: one JSON file shared by all jobs, owned by the main process
# ---------------------------------------------------------------------------

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
            raise LockError(f"another instance is already running{detail}; lock file: {self.path}") from None
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


# ---------------------------------------------------------------------------
# Incremental sync planning (pure: no network, no file writes)
# ---------------------------------------------------------------------------

REASON_NEW = "new"
REASON_RETRY = "retrying previously failed"
REASON_MISSING = "file missing on disk"
REASON_FORCED = "forced"


@dataclass
class Entry:
    """One video in a listing."""
    id: str
    title: str | None = None
    url: str | None = None
    duration: float | None = None
    upload_date: str | None = None  # YYYYMMDD
    uploader: str | None = None

    @property
    def label(self) -> str:
        return f"{self.title or '(title not fetched yet)'} [{self.id}]"


@dataclass
class Listing:
    entries: list[Entry]
    playlist_id: str | None = None
    playlist_title: str | None = None
    is_mix: bool = False


@dataclass
class SyncPlan:
    listed: int                                  # unique videos in the listing
    selected: list[Entry]                        # after `order` and `count`
    download: list[tuple[Entry, str]] = field(default_factory=list)  # (entry, reason)
    already: list[Entry] = field(default_factory=list)
    previously_failed: list[Entry] = field(default_factory=list)
    gone: list[tuple[str, str | None]] = field(default_factory=list)  # (id, title) not listed now

    @property
    def up_to_date(self) -> bool:
        return not self.download


def dedupe_entries(entries) -> list[Entry]:
    seen, unique = set(), []
    for entry in entries:
        if entry.id not in seen:
            seen.add(entry.id)
            unique.append(entry)
    return unique


def order_entries(entries, order: str, upload_dates: dict | None = None) -> list[Entry]:
    """Sort a listing by `order`. Sorting is stable, so ties keep playlist order;
    entries missing the sort key (unknown date or duration) go last."""
    entries = list(entries)
    if order == "playlist":
        return entries
    if order in ("title_asc", "title_desc"):
        return sorted(entries, key=lambda e: (e.title or "").casefold(), reverse=order == "title_desc")
    if order in ("newest", "oldest"):
        dates = upload_dates or {}
        key = lambda e: e.upload_date or dates.get(e.id)  # noqa: E731
    elif order in ("shortest", "longest"):
        key = lambda e: e.duration  # noqa: E731
    else:
        raise ValueError(f"unknown order {order!r}")
    known = [e for e in entries if key(e) is not None]
    unknown = [e for e in entries if key(e) is None]
    known.sort(key=key, reverse=order in ("newest", "longest"))
    return known + unknown


def entries_missing_dates(entries, order: str, upload_dates: dict) -> list[Entry]:
    """Entries whose upload date must be looked up before sorting by date."""
    if order not in ("newest", "oldest"):
        return []
    return [e for e in entries if not (e.upload_date or upload_dates.get(e.id))]


def plan_sync(entries, job_entry: dict | None, fmt: str, *, output_dir: Path,
              count: int | None = None, order: str = "playlist",
              upload_dates: dict | None = None, retry_failed: bool = False,
              force: bool = False, verify_files: bool = True,
              report_removed: bool = True) -> SyncPlan:
    """Decide what to download, by video ID and format.

    The listing is de-duplicated, sorted by `order` and cut to the first
    `count` entries. A selected video is skipped only if it has a
    `downloaded` record in this format (and, with verify_files, its file
    still exists). Failed videos are retried only with retry_failed.
    """
    unique = dedupe_entries(entries)
    ordered = order_entries(unique, order, upload_dates)
    selected = ordered if count is None else ordered[:count]
    videos = (job_entry or {}).get("videos", {})
    plan = SyncPlan(listed=len(unique), selected=selected)

    for entry in selected:
        record = videos.get(entry.id, {}).get(fmt)
        status = record.get("status") if record else None
        if force:
            plan.download.append((entry, REASON_FORCED))
        elif status == STATUS_DOWNLOADED:
            path = record.get("file_path")
            if verify_files and not (path and (Path(output_dir) / path).is_file()):
                plan.download.append((entry, REASON_MISSING))
            else:
                plan.already.append(entry)
        elif status == STATUS_FAILED:
            if retry_failed:
                plan.download.append((entry, REASON_RETRY))
            else:
                plan.previously_failed.append(entry)
        else:
            plan.download.append((entry, REASON_NEW))

    if report_removed:
        listed_ids = {e.id for e in unique}
        for video_id, formats in videos.items():
            if video_id not in listed_ids:
                title = next((r.get("title") for r in formats.values() if r.get("title")), None)
                plan.gone.append((video_id, title))
    return plan


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
