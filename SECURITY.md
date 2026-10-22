# Security Policy

## Supported versions

This is a small personal tool. Only the latest released version gets fixes.
Check yours with `python downloader.py --version`.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private
vulnerability reporting instead: open the repository's **Security** tab and
choose **Report a vulnerability**. Include:

- the version (`python downloader.py --version`) and your OS,
- steps to reproduce, ideally with a minimal `config.yaml`,
- what an attacker could do with it.

You should get a reply within 7 days. Fixes ship as a PATCH release and are
listed in `CHANGELOG.md`.

## Things to know when using the tool

**Cookies files are passwords.** `cookies_file` contains a logged-in YouTube
session. Keep it out of the repository (names matching `cookies*.txt` and
`*.cookies` are gitignored), restrict it with `chmod 600`, and log out of the
browser session to invalidate it if it leaks. The tool only reads this file: every yt-dlp
instance gets an in-memory copy, so parallel downloads can't overwrite or
garble it.

**Keep credentials out of `config.yaml`.** A `proxy` URL can contain a user name
and password. Put such settings in a personal copy such as `config.local.yaml`
(gitignored) and run with `--config config.local.yaml`.

**The metadata file is private data.** It lists the titles, URLs and local
paths of everything you downloaded. It is gitignored by default; don't publish
it.

**JavaScript runtime.** yt-dlp runs YouTube's player challenge code in an
external JavaScript runtime. It runs deno without any file or network
permissions and node with Node's `--permission` model; bun runs without such
restrictions. `setup.sh` and `js_runtime: auto` prefer deno, then node, then bun.

**Config files are trusted input.** The config is read with `yaml.safe_load`
(no Python object construction) and every key and value is validated; unknown
keys are rejected. Still, only run configs you wrote or reviewed.

**File system safety.**

- Job tags are reduced to safe folder names (no path separators, no `..`,
  no reserved Windows names).
- `filename_template` must be relative and cannot contain `..`, so files stay
  inside the job's folder.
- Playlist titles used for subfolders and video titles used in file names are
  sanitized.
- The tool never deletes downloaded files. Videos removed from a playlist are
  only reported.

**No shell commands.** ffmpeg and ffprobe are only started by yt-dlp with
argument lists, never through a shell.

**Locking protects against accidents, not attackers.** The lock file and atomic
writes stop two runs from corrupting the metadata file. Anyone who can write to
the output folder can still edit or replace it.

**Keep dependencies current.** YouTube changes often and old yt-dlp versions
break or carry known bugs. Update with
`uv pip install --python .venv/bin/python -U "yt-dlp[default]"` and install
ffmpeg from your OS package manager.

Only download content you have the right to download.
