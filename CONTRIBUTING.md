# Contributing

Thanks for helping improve youtube-downloader. This guide covers setting up,
the branch model, what a change needs, and how to open a pull request.

Please keep the project's purpose in mind: it downloads content its users are
allowed to download. Changes meant to get around DRM or other access controls
won't be accepted.

## Reporting bugs and requesting features

Open an issue and include:

- the version (`python downloader.py --version`), your OS and Python version,
  and the yt-dlp version (`.venv/bin/python -m yt_dlp --version`),
- the relevant part of your `config.yaml`, with cookies and proxy credentials
  removed,
- the command you ran and its output.

For a larger change, open an issue first so the approach can be agreed on
before you write the code.

Report security problems privately as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## Setting up

Fork the repository on GitHub, then:

```bash
git clone <your fork>
cd youtube-downloader
git remote add upstream <this repository's URL>
git checkout develop
./setup.sh --dev
.venv/bin/python -m pytest -m "not live"
```

See the README for what `setup.sh` installs and checks (uv, ffmpeg, a
JavaScript runtime). The tests that run the download pool need permission to
create POSIX semaphores; some sandboxed environments block that.

## Branches

| Branch | Purpose |
|---|---|
| `develop` | Integration branch. Pull requests target this branch. |
| `master` | Released versions, managed by the maintainers. |
| `feature/<short-name>` | Your branch for a new feature, from `develop`. |
| `fix/<short-name>` | Your branch for a bug fix, from `develop`. |

- Start every branch from an up-to-date `develop`:
  `git fetch upstream && git checkout -b feature/short-name upstream/develop`.
- One feature or fix per branch. Prefer several small pull requests to one
  large one.
- Don't commit to `develop` or `master`, and don't open pull requests
  against `master`.

## Making a change

1. Write code that reads like the code around it: type hints, small
   functions, the standard library before new dependencies. Two design rules
   to keep:
   - all yt-dlp calls live in `src/youtube_downloader/backend.py`;
   - only the main process reads or writes the metadata file. Download
     workers return results and the main process records them.
2. Add or update tests for what you changed. A bug fix comes with a test that
   fails without the fix.
3. Update the docs. Every config key and command-line flag must be described
   in `README.md`; a test checks this. Update `SECURITY.md` when a change
   affects security.
4. Bump the version in `manifest.json` (and its `updated` date), following
   the rules below, and add an entry to `CHANGELOG.md` under a heading
   `## [x.y.z] - YYYY-MM-DD`. A test checks that the current version has an
   entry.
5. Run the full offline test suite.

## Versioning

The project uses [semantic versioning](https://semver.org/):

- a new feature bumps MINOR,
- a bug fix bumps PATCH,
- a breaking change to the config format or the metadata format bumps MAJOR
  (MINOR while below 1.0.0). Describe it in the README and the changelog. A
  metadata change also bumps `schema_version` in
  `src/youtube_downloader/metadata.py`.

If another pull request takes your version number first, bump to the next
one when you update your branch.

## Commits

- Make small, logical commits.
- Write the subject in the imperative mood, at most 72 characters, for
  example `Add metadata store with atomic writes`. Add a short body only when
  the reason for the change isn't obvious.
- Don't add trailers such as `Co-authored-by` or `Signed-off-by`, "Generated
  with" lines, or emoji.

## Tests

```bash
.venv/bin/python -m pytest -m "not live"   # required, works offline
.venv/bin/python -m pytest -m live         # optional, needs access to YouTube
```

The offline suite replaces yt-dlp's network calls with `tests/fakes.py` but
runs the real download pool, metadata file and lock. Live tests talk to
YouTube and are skipped if YouTube is blocking your network.

## Pull requests

1. Bring your branch up to date with `develop` and resolve any conflicts on
   your branch:

   ```bash
   git fetch upstream
   git merge upstream/develop
   ```

2. Push the branch to your fork and open a pull request against `develop`.
3. In the description, say what changes and why, link the issue it closes,
   and note anything reviewers should check by hand (for example a live
   download you tried).
4. Before asking for review, check that:
   - the offline test suite passes,
   - new behavior has tests,
   - `README.md` describes any new config key, flag or behavior,
   - `manifest.json` is bumped and `CHANGELOG.md` has an entry,
   - commit messages follow the rules above.
5. Answer review comments with new commits on the same branch rather than
   force-pushing, so reviewers can see what changed.

A maintainer merges the pull request into `develop` with a merge commit
(`--no-ff`), so every change stays visible as its own merge. Pull requests
are not squashed. Releases are made by the maintainers.
