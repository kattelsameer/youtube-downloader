# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/): a new feature bumps MINOR, a bug
fix bumps PATCH, and a breaking change to the config or metadata format bumps
MAJOR (MINOR while below 1.0.0).

## [0.1.0] - 2026-10-23 - First release

### Configuration

- `config.yaml` with a `defaults` block and a list of `jobs`. Merge order:
  command line > job > defaults > built-in defaults.
- The whole config is validated at startup. Every problem (unknown key with
  a suggestion, bad value, missing URL, invalid `count`, duplicate tag, a
  folder given as `metadata_file`, missing `cookies_file`) is reported at
  once, naming the job.
- Job tags are turned into safe folder names, and tags that map to the same
  folder are rejected.
- URLs must be YouTube URLs that fit the job's mode. Mix playlists
  (`list=RD...`) are treated as normal playlists, capped at `mix_limit`;
  YouTube Music curated playlists (`RDCLAK5uy_...`) are not capped.
- `order` (playlist, newest, oldest, title_asc, title_desc, shortest,
  longest) sorts a playlist before `count` takes the first N.
- `filename_template` must stay inside the job folder, including Windows
  drive and `..\` forms.

### Downloading

- mp3: best audio converted with ffmpeg at `audio_quality`. mp4: best stream
  at or below `max_resolution`, falling back to the closest one, preferring
  h264/AAC, merged into mp4.
- Files go to `<output_dir>/<tag>/`, or `<output_dir>/<tag>/<playlist
  title>/` with `use_playlist_subfolder`.
- Work happens in `.incomplete/<video id>/` inside the job folder; only
  finished files are moved into the job folder. Leftovers of an interrupted
  attempt are cleared before the next one, except resumable `.part` files.
- `--force` and `skip_existing: false` replace a file only after the new
  download succeeds.
- Jobs run one after another; the videos of a job download in parallel in
  `max_workers` processes, each with its own YoutubeDL, plus
  `concurrent_fragment_downloads` per video.
- JavaScript runtime selection for yt-dlp (`js_runtime: auto` uses deno,
  then node, then bun).
- `cookies_file` (only read; each yt-dlp instance gets an in-memory copy),
  `proxy`, `rate_limit`, `retries`, `socket_timeout`, `embed_metadata`,
  `embed_thumbnail`.

### Incremental sync and metadata

- One JSON metadata file for all jobs (`schema_version` 1), with a record
  per video ID and per format.
- Each run downloads only what isn't recorded as downloaded in the job's
  current format, and prints "Up to date, nothing to download" when
  nothing is new.
- Failed videos are recorded with their error and attempt count, and
  skipped until `--retry-failed` or `retry_failed: true`.
- Switching a job's format downloads everything again and keeps the old
  format's records. `verify_files` re-downloads recorded videos whose file
  is gone; `--force` re-downloads everything selected.
- Videos removed from a playlist are reported as "no longer in playlist"
  and never deleted. Metadata of jobs removed from the config is kept.
- The metadata file is written atomically after every finished video and
  protected by a lock file against concurrent runs. A corrupt file is moved
  to `metadata.json.bak.<timestamp>` and the run starts fresh; folders and
  unreadable files are never moved, and a file from a newer schema is
  refused.

### Robustness

- A failing video or job never stops the others. If a worker process
  crashes, the videos it didn't finish are left for the next run instead of
  being marked as failed.
- Ctrl+C cancels queued downloads, stops running ones right away, keeps
  everything finished, and exits with code 130; the next run continues.
- Errors caused by YouTube rate limiting or bot checks come with a hint.
  yt-dlp's retry messages are logged at debug level.
- Output never crashes on characters the console can't encode.

### Command line

- `--config`, `--job` (repeatable), `--list`, `--dry-run`, `--status`,
  `--retry-failed`, `--force`, `--format`, `--output-dir`, `--max-workers`,
  `--version`.
- Per-video progress lines, a summary per job and an overall summary (new
  downloaded, already downloaded, failed, previously failed, no longer in
  playlist, time), with paths shown relative to the current folder.
- Exit codes: 0 ok, 1 some downloads failed, 2 config error, 3 another
  instance running, 4 metadata unusable, 130 interrupted.

### Project

- `setup.sh` sets up `.venv` with uv and checks for ffmpeg/ffprobe and a
  JavaScript runtime.
- Code in `src/youtube_downloader/`, with `downloader.py` as the entry
  point.
- `README.md`, `SECURITY.md`, this changelog, and `manifest.json` holding
  the version (`python downloader.py --version`).
- Offline test suite (fake yt-dlp with the real process pool, metadata file
  and lock) plus live tests against YouTube.
