# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

While the tool is being built, every merged feature or fix bumps the PATCH
number (0.0.x). Version 0.1.0 marks the first working release.

## [Unreleased]

## [0.0.15] - 2026-10-23

### Fixed

- A video interrupted during mp3 conversion could leave a truncated `.mp3`
  that the next run accepted as finished. Downloads and conversions now
  happen in `<job folder>/.incomplete/<video id>/`; only finished files are
  moved into the job folder, and leftovers except `.part` files are cleared
  before each attempt.
- `--force` (and `skip_existing: false`) deleted the existing file before
  downloading again, so a failed re-download lost it. The new file now
  replaces the old one only after it succeeds.

## [0.0.14] - 2026-10-23

### Added

- README: setup (uv, ffmpeg, JavaScript runtime), command line, config
  reference, output layout, metadata file format, incremental sync rules
  (including how `count` windows and Mix playlists behave), performance
  notes, Ctrl+C, troubleshooting, versioning and git workflow.
- Test that the README documents every setting and command-line flag.

## [0.0.13] - 2026-10-23

### Fixed

- Output shows paths relative to the current folder (download folder,
  lock file) instead of absolute paths.
- Dry runs no longer print an empty "already downloaded (0)" section.
- yt-dlp's "Retrying (n/3)" and "Giving up" warnings are logged at debug
  level; the final error is still reported once.

## [0.0.12] - 2026-10-23

### Fixed

- Ctrl+C now stops downloads that are in progress right away. Before, the
  worker processes were not terminated, so the tool waited for them to
  finish and their files were left on disk without metadata records.

## [0.0.11] - 2026-10-23

### Added

- Command line: `--config`, `--job` (repeatable), `--list`, `--dry-run`,
  `--status`, `--retry-failed`, `--force`, `--format`, `--output-dir`,
  `--max-workers`, `--version`. Exit codes: 0 ok, 1 some downloads
  failed, 2 config error, 3 another instance running, 4 metadata unusable,
  130 interrupted.
- Example `config.yaml` with commented defaults and mp3/mp4 single and
  playlist jobs (plus a disabled Mix job).

### Fixed

- Failed videos keep the title from the playlist listing in the metadata.

## [0.0.10] - 2026-10-23

### Added

- Job runner: jobs run one after another; each job's videos download in a
  `ProcessPoolExecutor` (`max_workers`), each worker with its own
  YoutubeDL. Only the main process writes the metadata file, flushing
  after every finished video.
- Ctrl+C cancels queued downloads, stops running workers and keeps
  everything finished so far; the next run continues with the rest.
- A failing video or job never stops the others; a crashed worker is
  recorded as a failure.
- Per-video progress lines, per-job summaries and an overall summary (new
  downloaded, already downloaded, failed, previously failed, no longer in
  playlist, time). "Up to date, nothing to download" when nothing is new.
- Optional `<tag>/<playlist title>/` subfolder (`use_playlist_subfolder`).

## [0.0.9] - 2026-10-23

### Added

- yt-dlp backend (`backend.py`): flat playlist listing (Mix playlists
  capped at `mix_limit`), exact upload-date lookups for date ordering, and
  the per-video download worker. mp3 uses `bestaudio` + FFmpegExtractAudio
  at `audio_quality`; mp4 prefers the best stream at or below
  `max_resolution` (falling back to the closest one), merged into mp4.
- JavaScript runtime selection (`js_runtime: auto` = deno, node, bun).
- Errors caused by YouTube rate limiting or bot checks carry a hint to wait,
  lower `max_workers` or use `cookies_file`.

## [0.0.8] - 2026-10-23

### Changed

- Code moved from one large `downloader.py` into the
  `src/youtube_downloader/` package (`version`, `config`, `metadata`,
  `sync`, `cli`). `downloader.py` is now a small entry point, so
  `python downloader.py ...` works as before.

## [0.0.7] - 2026-10-23

### Added

- Sync planner: sorts the listing by `order`, keeps the first `count`, and
  sorts each selected video into download (new, retry, file missing,
  forced), already downloaded, or previously failed, matching on video ID
  and format. Videos that dropped out of the playlist are reported as
  "no longer in playlist" and never touched.

## [0.0.6] - 2026-10-23

### Added

- Metadata store: one JSON file (`schema_version` 1) shared by all jobs,
  with records kept per video ID and per format, atomic writes (temp file,
  fsync, rename), a flush after every recorded video, and a lock file so two
  instances can't write at once.
- Corrupt or unreadable metadata is moved to `metadata.json.bak.<timestamp>`
  with a warning and the run starts fresh. Read-only commands leave the file
  alone. A file from a newer schema is refused instead of overwritten.

## [0.0.5] - 2026-10-23

### Fixed

- The uv dependency-resolve test is skipped instead of failing when PyPI
  can't be reached.

## [0.0.4] - 2026-10-23

### Added

- Config loader: `defaults` + `jobs` YAML with merge order CLI > job >
  defaults > built-in, validation of every key and value (all problems are
  reported at once, naming the job), unknown-key suggestions, tag
  sanitizing with duplicate detection, YouTube URL checks per mode, and the
  `order` setting (playlist, newest, oldest, title_asc, title_desc,
  shortest, longest).
- Mix/radio playlists (`list=RD...`) are accepted as normal playlists;
  `mix_limit` caps how many entries are read since mixes never end.

## [0.0.3] - 2026-10-23

### Added

- `CHANGELOG.md` and `SECURITY.md`.
- Test that fails when `manifest.json` has a version without a changelog entry.

## [0.0.2] - 2026-10-23

### Added

- `setup.sh`: creates `.venv` with uv, installs dependencies and checks for
  ffmpeg/ffprobe and a JavaScript runtime. Options: `--dev`,
  `--install-ffmpeg`, `--recreate`, `--python`.

## [0.0.1] - 2026-10-23

### Added

- Project scaffold: `manifest.json`, `requirements.txt`,
  `requirements-dev.txt`, `.gitignore` rules for downloads, metadata, lock
  files and cookies, and `downloader.py --version`.
