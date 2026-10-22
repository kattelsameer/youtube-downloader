# youtube-downloader

Download audio (mp3) or video (mp4) from YouTube, driven by a YAML file of
download jobs. Each job syncs one video or one playlist into its own folder.
A single metadata file remembers what every job has already downloaded, so
when a playlist gets new videos, the next run downloads only those.

```text
$ python downloader.py --config config.yaml
== song-playlist (playlist, mp3) ==
Playlist: 🎷 Copyright Free Jazz Music - 13 video(s) listed, order playlist, count 3 -> 3 selected
Up to date, nothing to download
```

Use it only for content you are allowed to download.

## Contents

- [Setup](#setup)
- [Quick start](#quick-start)
- [Command line](#command-line)
- [Config reference](#config-reference)
- [Where files go](#where-files-go)
- [The metadata file](#the-metadata-file)
- [Incremental sync rules](#incremental-sync-rules)
- [Performance](#performance)
- [Interrupting a run](#interrupting-a-run)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Versioning and changelog](#versioning-and-changelog)
- [Git workflow](#git-workflow)

## Setup

You need Python 3.10+, [uv](https://docs.astral.sh/uv/), ffmpeg (with
ffprobe) and, ideally, a JavaScript runtime for yt-dlp.

```bash
./setup.sh
```

`setup.sh` creates `.venv` with uv (downloading a suitable Python if none is
installed), installs `requirements.txt`, checks for ffmpeg/ffprobe and a
JavaScript runtime, and runs a smoke test. Options:

| Option | Effect |
|---|---|
| `--dev` | also install test dependencies (`requirements-dev.txt`) |
| `--install-ffmpeg` | install ffmpeg with brew, apt-get or dnf if it is missing |
| `--recreate` | delete and rebuild `.venv` |
| `--python 3.13` | Python version for uv to use |

Without `--install-ffmpeg`, the script asks before installing ffmpeg when run
in a terminal.

### ffmpeg

ffmpeg extracts mp3 audio and merges mp4 video and audio streams. Install it
with your package manager:

| OS | Command |
|---|---|
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt-get install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Windows | `winget install ffmpeg` |

### JavaScript runtime

YouTube now requires yt-dlp to solve JavaScript challenges. Without a runtime,
some formats are missing or downloads fail. Install [deno](https://deno.com/)
(`brew install deno`); node or bun also work. With `js_runtime: auto` the tool
uses deno, then node, then bun, whichever is found first. See
[SECURITY.md](SECURITY.md) for why deno is preferred.

### Manual setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## Quick start

```bash
source .venv/bin/activate
python downloader.py --config config.yaml --list      # what is configured
python downloader.py --config config.yaml --dry-run   # what would happen
python downloader.py --config config.yaml             # download
python downloader.py --config config.yaml             # again: "Up to date"
```

## Command line

| Command / flag | Behavior |
|---|---|
| `python downloader.py --config config.yaml` | Run all enabled jobs (`--config` defaults to `config.yaml`). |
| `--job TAG` | Run only this job. Repeat for several: `--job a --job b`. |
| `--list` | Show every job: tag, mode, format, enabled, videos tracked in the metadata, downloads in the current format. No downloading. |
| `--dry-run` | List the playlists and show what would be downloaded or skipped. Downloads nothing and doesn't change the metadata. |
| `--status [--job TAG]` | Metadata report per job: downloaded and failed counts per format, last run, and failed videos with their errors. Also shows jobs that are no longer in the config. |
| `--retry-failed` | Retry videos that failed on earlier runs. |
| `--force` | Ignore the metadata and download every selected video again, updating the records. |
| `--format mp3\|mp4` | Override the format of every selected job. |
| `--output-dir DIR` | Override `output_dir` (relative to the current folder). |
| `--max-workers N` | Override `max_workers`. |
| `--version` | Print the version from `manifest.json`. |

`--list`, `--dry-run` and `--status` can't be combined.

Exit codes: `0` everything worked, `1` some videos or jobs failed, `2` config
or argument error, `3` another instance is running, `4` the metadata file
can't be used safely (written by a newer version), `130` interrupted with
Ctrl+C.

## Config reference

`config.yaml` has two sections:

```yaml
defaults:          # settings every job inherits
  format: mp3
  max_workers: 3
jobs:              # list of jobs; each can override any default
  - tag: lofi-songs
    mode: playlist
    url: https://www.youtube.com/playlist?list=...
    count: 10
    format: mp4    # this job only
```

Precedence: **command-line flags > job settings > `defaults` > built-in
defaults**. The whole config is checked at startup. Every problem (unknown
key, bad value, missing URL, invalid count, duplicate tag) is reported at
once with the job it belongs to, and nothing is downloaded until the config is
valid:

```text
Config error in config.yaml:
  - jobs[0] (tag 'lofi'): format: must be one of mp3, mp4 (got 'flac')
  - jobs[1] (tag 'Lofi'): duplicate tag 'Lofi' (same folder 'Lofi' as jobs[0] (tag 'lofi'))
  - jobs[1] (tag 'Lofi'): count: must be a positive integer or "all" (got 0)
```

Relative paths in the config (`output_dir`, `cookies_file`,
`metadata_file`) are relative to the config file's folder. `--output-dir`
is relative to the current folder.

### Settings (in `defaults`, overridable per job)

| Key | Default | Meaning |
|---|---|---|
| `format` | `mp3` | `mp3`: audio only. `mp4`: video merged with audio in an mp4 file. |
| `audio_quality` | `192` | mp3 bitrate in kbps, 32 to 320. |
| `max_resolution` | `720` | mp4 height limit (`360`, `720`, `1080`, ... or `best`). Picks the best stream at or below it; if there is none, the closest available one. Prefers h264 + AAC for compatibility. |
| `output_dir` | `downloads` | Base folder, created if missing. |
| `filename_template` | `%(title)s [%(id)s].%(ext)s` | yt-dlp output template inside the job folder. Must contain `%(ext)s` and stay inside the folder. Keep `%(id)s` so videos with the same title don't collide. |
| `use_playlist_subfolder` | `false` | Save playlist jobs into `<output_dir>/<tag>/<playlist title>/`. |
| `order` | `playlist` | How a playlist is sorted before `count` is applied: `playlist` (YouTube's order), `newest`, `oldest`, `title_asc`, `title_desc`, `shortest`, `longest`. |
| `mix_limit` | `25` | How many entries to read from a Mix playlist (they never end). |
| `max_workers` | `3` | Videos downloaded at the same time within a job. |
| `concurrent_fragment_downloads` | `4` | Parts of one video downloaded at the same time. |
| `retries` | `3` | yt-dlp retries for network errors, fragments and extraction. |
| `socket_timeout` | `30` | Seconds before a stalled connection is dropped. |
| `rate_limit` | `null` | Speed limit per video, e.g. `2M` or `500K`. |
| `skip_existing` | `true` | If the output file already exists on disk, record it instead of downloading it again. |
| `verify_files` | `true` | If a video is recorded as downloaded but its file is gone, download it again. |
| `retry_failed` | `false` | Retry previously failed videos (same as `--retry-failed`). |
| `cookies_file` | `null` | Netscape `cookies.txt` for age-restricted videos or bot checks. |
| `proxy` | `null` | Proxy URL, e.g. `socks5://127.0.0.1:1080`. |
| `embed_metadata` | `true` | Write title/artist tags into the file. |
| `embed_thumbnail` | `false` | Embed the thumbnail as cover art. |
| `js_runtime` | `auto` | JavaScript runtime for yt-dlp: `auto`, `deno`, `node`, `bun`, `quickjs`, `none`. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

Global only (in `defaults`, not allowed in a job):

| Key | Default | Meaning |
|---|---|---|
| `metadata_file` | `<output_dir>/metadata.json` | The shared metadata file. |

### Job keys

| Key | Required | Meaning |
|---|---|---|
| `tag` | yes | Short unique label, used as the folder name and metadata key. It is sanitized for folder names (`Lofi Songs!` becomes `Lofi-Songs`); two tags that end up as the same folder (case-insensitive) are an error. |
| `mode` | yes | `single` (one video) or `playlist` (playlist or channel `/videos` URL). |
| `url` | yes | The video or playlist URL. Must be a YouTube URL that fits the mode. |
| `enabled` | no | `false` skips the job but keeps it (default `true`). |
| `count` | no | Playlist mode: a number N (first N videos after `order`) or `"all"` (default). Ignored, with a warning, in single mode. |
| `retry_failed` | no | Same as the default of the same name, for this job. |

Any setting from the table above can also be set on a job.

## Where files go

```text
downloads/
  metadata.json            shared by all jobs
  metadata.json.lock       held while a run is active
  song-single/
    Chaahat - Official 4K Version ... [10azSRvOv_E].mp3
  song-playlist/
    'Night Out' by @LiQWYD ... [FqxZwBLm_vk].mp3
  video-playlist/
    🎷 Copyright Free Jazz Music/   (only with use_playlist_subfolder: true)
```

Every job writes into `<output_dir>/<tag>/`. With `use_playlist_subfolder:
true`, playlist jobs write into `<output_dir>/<tag>/<playlist title>/`.

Downloads and conversions happen in a hidden `.incomplete/<video id>/`
folder inside the job folder. A file is moved into the job folder only after
it is completely downloaded and converted, so the job folder never holds a
half-written mp3 or mp4. The `.incomplete` folder is removed when a video
finishes; after an interruption it keeps yt-dlp's `.part` files for resuming.

## The metadata file

One JSON file, shared by all jobs, is the only source of truth for what has
been downloaded. yt-dlp's `download_archive` is not used.

```json
{
  "schema_version": 1,
  "app_version": "0.0.11",
  "updated_at": "2026-10-22T19:05:07+00:00",
  "jobs": {
    "song-playlist": {
      "url": "https://www.youtube.com/playlist?list=PLfP6i5T0-DkK52drjlLM2wEGXSL-rqF4T",
      "mode": "playlist",
      "format": "mp3",
      "playlist_id": "PLfP6i5T0-DkK52drjlLM2wEGXSL-rqF4T",
      "playlist_title": "🎷 Copyright Free Jazz Music",
      "last_run": "2026-10-22T19:05:07+00:00",
      "videos": {
        "FqxZwBLm_vk": {
          "mp3": {
            "title": "'Night Out' by @LiQWYD  🇸🇪 | Saxophone Vlog Music (No Copyright) 🎷",
            "uploader": "BreakingCopyright — Royalty Free Music",
            "duration": 193,
            "upload_date": "20190221",
            "format": "mp3",
            "file_path": "song-playlist/'Night Out' by @LiQWYD  🇸🇪 ｜ Saxophone Vlog Music (No Copyright) 🎷 [FqxZwBLm_vk].mp3",
            "file_size": 4638472,
            "status": "downloaded",
            "downloaded_at": "2026-10-22T19:04:40+00:00",
            "attempts": 1,
            "last_error": null
          }
        }
      }
    }
  },
  "upload_dates": { "FqxZwBLm_vk": "20190221" }
}
```

(A real record from a test run, shortened to one video. Timestamps are UTC.)

| Field | Meaning |
|---|---|
| `schema_version` | Format version of this file. A tool that finds a newer version refuses to touch it (exit code 4). |
| `jobs.<tag>` | One entry per job tag: `url`, `mode`, `format` (latest used), `playlist_id`, `playlist_title`, `last_run`, `videos`. |
| `videos.<video id>.<format>` | One record per video **and format**: `title`, `uploader`, `duration`, `upload_date`, `format`, `file_path` (relative to `output_dir`), `file_size`, `status` (`downloaded` or `failed`), `downloaded_at`, `attempts`, `last_error`. |
| `upload_dates` | Cache of upload dates used by `order: newest/oldest`. |

Records are keyed by format because a job can switch between mp3 and mp4.
The old format's records stay, and switching back doesn't download anything
again.

How the file is kept safe:

- **One writer.** Only the main process reads and writes it. Download
  workers return their results, and the main process records them.
- **Atomic writes.** Each save writes a temp file in the same folder, fsyncs
  it and renames it over the old one, so readers never see half a file.
- **Saved after every video**, so a crash or Ctrl+C loses nothing that
  finished.
- **Lock file.** `metadata.json.lock` is locked for the whole run. A second
  instance exits with code 3 and says which process holds the lock. The OS
  releases the lock if the process dies, so it never goes stale. `--list`,
  `--status` and `--dry-run` only read and don't take the lock.
- **Corruption.** If the file can't be parsed or has the wrong structure, it
  is moved to `metadata.json.bak.<timestamp>`, a warning is printed, and the
  run starts with empty metadata. Read-only commands only warn and leave the
  file alone.
- **Removed jobs.** If you delete a job from the config, its metadata stays
  untouched; `--status` lists it as "not in config any more".

Format changes: the metadata format is versioned with `schema_version`. A
breaking change to it, or to the config format, bumps the MAJOR version
(MINOR while below 1.0.0) and is described here and in the changelog.

## Incremental sync rules

Every run of a job does this:

1. **List.** A playlist job reads the current playlist with yt-dlp's
   `extract_flat`: titles and IDs only, about one request per 100 videos. A
   single job needs no request: the video ID is in the URL.
2. **Sort** by `order`. `newest` and `oldest` need upload dates, which the
   playlist listing doesn't include. They are looked up once per video, cached
   in `upload_dates`, and videos without a known date sort last.
3. **Window.** Keep the first `count` videos (all with `count: all`).
4. **Compare by video ID and format.** A video counts as downloaded only if
   there is a `downloaded` record for the same video ID **and** the same
   format. Videos are never matched by title or position, since both can
   change.
5. **Download** what is not downloaded yet. If nothing is new, the job prints
   **"Up to date, nothing to download"**.

Details:

- **Failed videos** are recorded with `status: failed`, `last_error` and
  `attempts`. Later runs report them as "previously failed" and skip them,
  unless you pass `--retry-failed` or set `retry_failed: true`.
- **Videos removed from the playlist** are never deleted: their files and
  metadata stay. The summary counts them as "no longer in playlist".
- **`count: N` is a window over the current playlist.** If the playlist
  owner adds 2 videos at the top of a `count: 10` job, the window now holds
  those 2 plus the first 8 old ones. The 2 new ones are downloaded, and the
  2 old ones that fell out of the window are not re-checked. They still
  exist in the playlist, so they aren't "no longer in playlist"; their
  files and records stay. If the playlist grows at the bottom, a `count: N`
  window doesn't see the new videos. Use `count: all`, or `order: newest`
  if what you want is the latest uploads.
- **Count larger than the playlist** takes every video and says so.
- **Format change** (mp3 to mp4): every video is downloaded again in the new
  format; mp3 files and records stay. Changing only `max_resolution` or
  `audio_quality` doesn't trigger a new download. Use `--force` for that.
- **Deleted files**: with `verify_files: true`, a recorded video whose file
  is missing is downloaded again. With `false`, the record is trusted.
- **`--force`** downloads every selected video again and updates the records.
  The old file is replaced only after the new download has succeeded; if it
  fails, the old file stays.
- **Single mode** follows the same rules for its one video.
- **Several jobs** share the metadata file but each has its own entry, so
  the same video in two jobs is downloaded once per job, into each job's
  folder.

### Mix playlists

Auto-generated Mix/radio playlists (`list=RD...`) are treated like normal
playlists, with two differences: YouTube returns a different selection on
almost every fetch, and the list never ends. The tool reads at most
`mix_limit` entries and applies `order` and `count` to those. Tracking by
video ID works as usual, but a later run may see new songs in the window and
download them, and earlier songs may show up as "no longer in playlist".
For a fixed set of songs, save the mix as a regular playlist on YouTube and
use that.

## Performance

- **Jobs run one after another; the videos inside a job download in
  parallel.** Each job uses a `ProcessPoolExecutor` with `max_workers`
  processes, and each worker creates its own `YoutubeDL`. Running jobs one at
  a time keeps the load on YouTube predictable (fewer rate limits and bot
  checks), keeps each job's output and summary together, and gives each job
  its own `max_workers`.
- **`concurrent_fragment_downloads`** downloads several parts of one video at
  once, which speeds up segmented streams.
- Measured on this machine: 3 mp3 tracks took 27.6s with `max_workers: 1`
  and 10.6s with `max_workers: 3`.
- **Listing is cheap; `order: newest/oldest` is not**, at first. Sorting by
  date needs one metadata request per video. The dates are cached, so later
  runs only look up new videos.

## Interrupting a run

Ctrl+C cancels queued downloads and stops the running ones right away.
Every video that finished before that is already in the metadata. The run
exits with code 130, and the next run continues with the rest. yt-dlp keeps
its `.part` files in `.incomplete/`, so a half-downloaded video resumes where
it stopped. Anything else left there by the interruption, such as an mp3
whose conversion was cut off, is deleted before the next attempt, so a
truncated file is never mistaken for a finished one.

## Troubleshooting

**"Sign in to confirm you're not a bot" or "HTTP Error 429".** YouTube is
rate-limiting your network. The tool adds a hint to these errors. Wait a
while, lower `max_workers`, or export your YouTube cookies to a
`cookies.txt` file (for example with a "Get cookies.txt" browser extension)
and set `cookies_file`. Treat that file like a password; see
[SECURITY.md](SECURITY.md).

**"No supported JavaScript runtime could be found".** Install deno or node,
or set `js_runtime` to the one you have.

**"ffmpeg not found" / postprocessing errors.** Install ffmpeg (see Setup) and
make sure `ffmpeg` and `ffprobe` are on your PATH.

**"another instance is already running".** Another run holds the lock. Wait
for it to finish. The lock file itself can stay; it isn't stale.

**A playlist job says "lists playlists or channel tabs, not videos".** Use the
channel's `/videos` URL (for example `https://www.youtube.com/@name/videos`)
instead of the bare channel URL.

## Project layout

```text
downloader.py               entry point (python downloader.py ...)
src/youtube_downloader/
  config.py                 load, validate and merge config.yaml
  metadata.py               metadata file: atomic writes, lock, recovery
  sync.py                   decide what to download (pure logic)
  backend.py                yt-dlp listing, upload dates, download worker
  runner.py                 process pool, recording results, summaries
  cli.py                    command-line interface
  logs.py, version.py       logging setup, version from manifest.json
tests/                      pytest suite (fakes.py replaces yt-dlp)
config.yaml                 example config
setup.sh                    environment setup with uv
manifest.json               name, version, date
CHANGELOG.md, SECURITY.md
```

## Tests

```bash
./setup.sh --dev
.venv/bin/python -m pytest -m "not live"     # offline suite
.venv/bin/python -m pytest -m live           # talks to YouTube
```

The offline suite replaces yt-dlp's listing and download calls with
`tests/fakes.py`, but still runs the real process pool, metadata file and
lock. It covers config validation and merging, tag sanitizing, the metadata
store (atomic writes, corruption recovery, lock), every incremental sync
scenario (re-run, growing playlist, removed video, failures and
`--retry-failed`, format change, `verify_files`, Ctrl+C and resume, two
instances at once, several jobs in one file), and the CLI. Tests marked
`live` need network access to YouTube.

### Live checks to run yourself

```bash
python downloader.py --config config.yaml --dry-run
python downloader.py --config config.yaml           # downloads
python downloader.py --config config.yaml           # "Up to date" for every job
python downloader.py --config config.yaml --status
ffprobe -v error -show_entries stream=codec_name,width,height,bit_rate -of compact "downloads/video-single/"*.mp4
```

## Versioning and changelog

`manifest.json` holds the `name`, `version` (semantic versioning,
`MAJOR.MINOR.PATCH`) and `updated` date; `python downloader.py --version`
prints it. Every feature or fix branch bumps the version before it is merged
into `develop` and adds an entry to [CHANGELOG.md](CHANGELOG.md):

- During initial development, each merged change bumped the PATCH number
  (0.0.1, 0.0.2, ...). 0.1.0 is the first working release.
- From 0.1.0 on: a new feature bumps MINOR, a bug fix bumps PATCH, and a
  breaking change to the config or metadata format bumps MAJOR (MINOR while
  below 1.0.0) and is described in this README.

## Git workflow

- `develop` is the integration branch, `master` the release branch.
  Releasing to `master` is done by hand by the maintainer.
- Every change gets its own branch from `develop`: `feature/<name>` or
  `fix/<name>`, built as small focused branches with small commits.
- Nothing is committed directly on `develop` or `master`. The only commits
  on `develop` are `--no-ff` merges of finished branches, which keep each
  feature visible in the history.
- A branch is merged only when its tests and the whole existing suite pass
  and `manifest.json` has been bumped. Conflicts are resolved on the feature
  branch by merging `develop` into it first.
- Commit messages are a single imperative subject line of at most 72
  characters, with a short body only when needed, and no trailers.

## License

MIT, see [LICENSE](LICENSE).
