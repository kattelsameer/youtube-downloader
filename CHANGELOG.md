# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

While the tool is being built, every merged feature or fix bumps the PATCH
number (0.0.x). Version 0.1.0 marks the first working release.

## [Unreleased]

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
