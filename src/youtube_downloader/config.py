"""Config loading, validation and merging (CLI > job > defaults > built-in)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml


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

    metadata_path = None
    metadata_file = defaults_raw.get("metadata_file")
    if metadata_file not in (None, ""):
        if not isinstance(metadata_file, str):
            errors.append(f"defaults: metadata_file: must be a file path (got {metadata_file!r})")
        else:
            metadata_path = Path(_resolve_path(metadata_file, base))
            if metadata_path.is_dir():
                errors.append(f"defaults: metadata_file: {metadata_file!r} is a folder; "
                              f"give a file path such as downloads/metadata.json")

    if errors:
        raise ConfigError(errors)

    merged_defaults = {**defaults, **cli}
    if "output_dir" not in cli:
        merged_defaults["output_dir"] = _resolve_path(merged_defaults["output_dir"], base)
    if metadata_path is None:
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
