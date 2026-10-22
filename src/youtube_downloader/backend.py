"""yt-dlp backend: playlist listings and upload dates (main process) and the
download worker that runs in child processes.

Every function creates its own YoutubeDL instance; nothing yt-dlp related is
shared between processes or threads.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import logging
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp

from .config import VIDEO_ID_RE, Job, is_mix_playlist, parse_youtube_url
from .sync import Entry, Listing

log = logging.getLogger("ytdl")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RETRY_NOISE_RE = re.compile(r"Retrying \(\d+/\d+\)|Giving up after \d+ retries")
MAX_ERROR_LEN = 500


class ListingError(Exception):
    """The playlist or channel could not be listed."""


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def clean_error(exc: BaseException | str) -> str:
    """One readable line from a yt-dlp error (no colors, no 'ERROR:' prefix)."""
    text = ANSI_RE.sub("", str(exc)).strip()
    text = re.sub(r"^(ERROR:\s*)+", "", text)
    text = " ".join(text.split())
    return text[:MAX_ERROR_LEN] or type(exc).__name__


BLOCK_SIGNS = ("not a bot", "http error 429", "too many requests")
BLOCK_HINT = ("YouTube is rate-limiting or bot-checking this network: wait a while, "
              "lower max_workers, or set cookies_file (see README)")


def blocked_hint(error: str | None) -> str | None:
    """A how-to-fix hint when an error means YouTube is blocking us."""
    text = (error or "").lower()
    return BLOCK_HINT if any(sign in text for sign in BLOCK_SIGNS) else None


def _date_from_timestamp(timestamp) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y%m%d")


class YtdlLogger:
    """Routes yt-dlp output into our logger. Errors are only logged at debug
    level because they are raised and reported once per video anyway."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def debug(self, msg: str) -> None:
        log.debug("%s%s", self.prefix, msg)

    info = debug

    def warning(self, msg: str) -> None:
        msg = ANSI_RE.sub("", msg).removeprefix("WARNING: ")
        level = logging.DEBUG if RETRY_NOISE_RE.search(msg) else logging.WARNING
        log.log(level, "%s%s", self.prefix, msg)

    def error(self, msg: str) -> None:
        log.debug("%s%s", self.prefix, ANSI_RE.sub("", msg))


def js_runtimes_option(choice: str) -> dict:
    """yt-dlp `js_runtimes`. 'auto' uses deno, then node, then bun from PATH."""
    if choice == "none":
        return {}
    if choice == "auto":
        for name in ("deno", "node", "bun"):
            if shutil.which(name):
                return {name: {}}
        return {}
    return {choice: {}}


def base_ydl_options(settings: dict, prefix: str = "") -> dict:
    """Network and logging options shared by listing and downloading."""
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": YtdlLogger(prefix),
        "socket_timeout": settings["socket_timeout"],
        "retries": settings["retries"],
        "extractor_retries": settings["retries"],
        "js_runtimes": js_runtimes_option(settings["js_runtime"]),
    }
    if settings.get("cookies_file"):
        # yt-dlp writes the cookie jar back to `cookiefile` when it closes. Many
        # workers doing that at once could garble the user's file, so each
        # instance gets an in-memory copy and the file on disk is never written.
        opts["cookiefile"] = io.StringIO(Path(settings["cookies_file"]).read_text(encoding="utf-8"))
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    return opts


# ---------------------------------------------------------------------------
# Listing (main process)
# ---------------------------------------------------------------------------

def _entry_from_info(item: dict) -> Entry:
    return Entry(
        id=item["id"],
        title=item.get("title"),
        url=watch_url(item["id"]),
        duration=item.get("duration"),
        upload_date=item.get("upload_date") or _date_from_timestamp(item.get("timestamp")),
        uploader=item.get("uploader") or item.get("channel"),
    )


def fetch_listing(job: Job) -> Listing:
    """List the videos a job covers.

    Single mode needs no network: the video ID is in the URL. Playlists are
    read with extract_flat (one request per ~100 videos). Mixes never end,
    so only the first `mix_limit` entries are read.
    """
    video_id, playlist_id = parse_youtube_url(job.url)
    if job.mode == "single":
        return Listing(entries=[Entry(id=video_id, url=watch_url(video_id))])

    mix = is_mix_playlist(playlist_id)
    opts = base_ydl_options(job.settings, prefix=f"[{job.tag}] ")
    opts.update(extract_flat="in_playlist", skip_download=True)
    if mix:
        opts["playlistend"] = job.settings["mix_limit"]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job.url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ListingError(clean_error(exc)) from None
    if not info:
        raise ListingError("yt-dlp returned no information for this URL")

    if "entries" not in info:  # the URL resolved to one video
        return Listing(entries=[_entry_from_info(info)], is_mix=mix)

    entries, nested = [], 0
    for item in info.get("entries") or []:
        if not item or not item.get("id"):
            continue
        if item.get("ie_key") not in (None, "Youtube") or not VIDEO_ID_RE.fullmatch(item["id"]):
            nested += 1  # e.g. the Videos/Shorts tabs of a bare channel URL
            continue
        entries.append(_entry_from_info(item))
    if nested and not entries:
        raise ListingError("this URL lists playlists or channel tabs, not videos; "
                           "use a playlist URL or the channel's /videos URL")
    return Listing(entries=entries, playlist_id=info.get("id") or playlist_id,
                   playlist_title=info.get("title"), is_mix=mix)


def lookup_upload_dates(entries, settings: dict, max_threads: int = 8) -> dict[str, str]:
    """Exact upload dates (YYYYMMDD) for entries, one metadata request each,
    run in a few threads. Videos that fail to resolve are left out."""
    def one(entry: Entry):
        opts = base_ydl_options(settings)
        opts.update(skip_download=True, noplaylist=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(watch_url(entry.id), download=False, process=False)
        except Exception as exc:  # noqa: BLE001 - one bad video must not stop the rest
            log.debug("no upload date for %s: %s", entry.id, clean_error(exc))
            return entry.id, None
        info = info or {}
        return entry.id, info.get("upload_date") or _date_from_timestamp(info.get("timestamp"))

    entries = list(entries)
    if not entries:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_threads, len(entries))) as pool:
        return {vid: date for vid, date in pool.map(one, entries) if date}


# ---------------------------------------------------------------------------
# Downloading (worker processes)
# ---------------------------------------------------------------------------

STAGING_DIR = ".incomplete"
RESUMABLE_SUFFIXES = (".part", ".ytdl")


def staging_dir(task: dict) -> Path:
    """Per-video work folder inside the job folder. yt-dlp downloads and
    converts there, so the job folder only ever receives finished files."""
    return Path(task["target_dir"]) / STAGING_DIR / task["video_id"]


def replaces_existing(task: dict) -> bool:
    """--force or skip_existing: false: download again even if the file exists."""
    return bool(task.get("force")) or not task["settings"]["skip_existing"]


def clear_stale_outputs(staging: Path) -> None:
    """Delete leftovers of an interrupted attempt except resumable .part files.

    ffmpeg writes converted audio straight to its final name, so a killed
    conversion leaves a truncated .mp3 that yt-dlp would take as finished.
    """
    if not staging.is_dir():
        return
    for item in staging.rglob("*"):  # templates may create subfolders
        if item.is_file() and not item.name.endswith(RESUMABLE_SUFFIXES):
            item.unlink()


def remove_staging(staging: Path, everything: bool) -> None:
    if everything:
        shutil.rmtree(staging, ignore_errors=True)
    elif staging.is_dir():
        for folder in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):
                folder.rmdir()  # only succeeds when empty
    for folder in (staging, staging.parent):
        with contextlib.suppress(OSError):
            folder.rmdir()


def build_download_options(task: dict) -> dict:
    """yt-dlp options for one video, from the job's merged settings."""
    s = task["settings"]
    staging = staging_dir(task)
    replace = replaces_existing(task)
    opts = base_ydl_options(s, prefix=f"[{task['tag']}] ")
    opts.update({
        "outtmpl": {"default": s["filename_template"]},
        # Work happens in the staging folder; yt-dlp moves the finished file to
        # "home". When replacing, the worker moves it itself after success, so
        # a failed re-download never costs the old file.
        "paths": {"home": str(staging if replace else Path(task["target_dir"])), "temp": str(staging)},
        "noplaylist": True,
        "fragment_retries": s["retries"],
        "concurrent_fragment_downloads": s["concurrent_fragment_downloads"],
        "continuedl": True,
        # None = keep an existing finished file (yt-dlp: "already downloaded")
        "overwrites": True if replace else None,
    })
    if s.get("rate_limit"):
        opts["ratelimit"] = s["rate_limit"]

    postprocessors = []
    if s["format"] == "mp3":
        opts.update(format="bestaudio/best", final_ext="mp3")
        postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                               "preferredquality": str(s["audio_quality"])})
    else:
        height = s["max_resolution"]
        # res:N prefers the best stream at or below N and falls back to the
        # closest available one; mp4/m4a and h264 keep the result widely playable.
        opts.update(
            format="bv*+ba/b",
            format_sort=["res" if height == "best" else f"res:{height}", "ext:mp4:m4a", "vcodec:h264"],
            merge_output_format="mp4",
            final_ext="mp4",
        )
        postprocessors.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"})
    if s["embed_metadata"]:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
    if s["embed_thumbnail"]:
        opts["writethumbnail"] = True
        postprocessors.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg",
                                  "when": "before_dl"})
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    opts["postprocessors"] = postprocessors
    return opts


def download_worker(task: dict) -> dict:
    """Download one video. Runs in a worker process and never raises: the
    result dict goes back to the main process, which records it."""
    started = time.monotonic()
    video_id = task["video_id"]
    result = {"video_id": video_id, "ok": False, "title": task.get("title"), "error": None}
    final_paths: list[str] = []
    announced: list[bool] = []

    def on_progress(status: dict) -> None:
        if status.get("status") == "downloading" and not announced:
            announced.append(True)
            info = status.get("info_dict") or {}
            log.info("[%s] started     %s [%s]", task["tag"], info.get("title") or task.get("title") or "", video_id)

    staging = staging_dir(task)
    opts = build_download_options(task)
    opts["post_hooks"] = [final_paths.append]
    opts["progress_hooks"] = [on_progress]
    try:
        clear_stale_outputs(staging)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url(video_id), download=True)
        if not info:
            raise RuntimeError("yt-dlp returned no information")
        downloads = info.get("requested_downloads") or [{}]
        path = final_paths[-1] if final_paths else downloads[-1].get("filepath")
        if not path or not Path(path).is_file():
            raise RuntimeError("download finished but the output file was not found")
        if replaces_existing(task):
            # keep any subfolders the filename template created
            destination = Path(task["target_dir"]) / Path(path).relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)  # same file system: atomic swap
            path = str(destination)
        remove_staging(staging, everything=True)
        result.update(
            ok=True,
            title=info.get("title"),
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            upload_date=info.get("upload_date"),
            file_path=Path(os.path.relpath(path, task["output_dir"])).as_posix(),
            file_size=Path(path).stat().st_size,
        )
    except Exception as exc:  # noqa: BLE001 - report every failure, keep the pool alive
        result["error"] = clean_error(exc)
        remove_staging(staging, everything=False)  # keep .part files for a resume
    result["elapsed"] = round(time.monotonic() - started, 2)
    return result
