# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

While the tool is being built, every merged feature or fix bumps the PATCH
number (0.0.x). Version 0.1.0 marks the first working release.

## [Unreleased]

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
