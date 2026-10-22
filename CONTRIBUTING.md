# Contributing

Thanks for helping improve youtube-downloader. This guide covers setting up,
the branch model, what a change needs before it is merged, and how releases
are made.

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

Report security problems privately as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## Setting up

```bash
git clone <your fork>
cd youtube-downloader
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
| `master` | Released versions. Each release is tagged `v<version>`. |
| `develop` | Integration branch. Every change is merged here first. |
| `feature/<short-name>` | A new feature, branched from `develop`. |
| `fix/<short-name>` | A bug fix, branched from `develop`. |
| `release-<version>` | Release preparation, branched from `develop`. |

- Start every branch from an up-to-date `develop`.
- One feature or fix per branch. Prefer several small branches to one large
  one.
- Never commit directly to `develop` or `master`.
- Open pull requests against `develop`.

## Making a change

1. Create a branch: `git checkout -b feature/short-name develop`.
2. Write code that reads like the code around it: type hints, small
   functions, the standard library before new dependencies. Two design rules
   to keep:
   - all yt-dlp calls live in `src/youtube_downloader/backend.py`;
   - only the main process reads or writes the metadata file. Download
     workers return results and the main process records them.
3. Add or update tests for what you changed. A bug fix comes with a test that
   fails without the fix.
4. Update the docs. Every config key and command-line flag must be described
   in `README.md`; a test checks this. Update `SECURITY.md` when a change
   affects security.
5. Bump the version in `manifest.json` (and its `updated` date), following
   the rules below, and add an entry to `CHANGELOG.md` under a heading
   `## [x.y.z] - YYYY-MM-DD`. A test checks that the current version has an
   entry.
6. Run the full offline test suite.

## Versioning

The project uses [semantic versioning](https://semver.org/):

- a new feature bumps MINOR,
- a bug fix bumps PATCH,
- a breaking change to the config format or the metadata format bumps MAJOR
  (MINOR while below 1.0.0). Describe it in the README and the changelog. A
  metadata change also bumps `schema_version` in
  `src/youtube_downloader/metadata.py`.

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

## Getting a change merged

- Merge `develop` into your branch and resolve any conflicts there, never on
  `develop`.
- The whole test suite passes, the version is bumped and the changelog has
  an entry.
- A maintainer merges the branch into `develop` with
  `git merge --no-ff <branch>`, so every change stays visible as its own
  merge. Branches are not squashed.

## Releases

Maintainers make releases:

1. `git checkout -b release-X.Y.Z develop`
2. Set `X.Y.Z` in `manifest.json` if it isn't already, tidy the changelog
   entry for the release and check the docs.
3. Run the full test suite, including the live tests.
4. Merge `release-X.Y.Z` into `develop`, then `develop` into `master`, both
   with `--no-ff`.
5. Tag the release on `master` and push:

   ```bash
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin master develop vX.Y.Z
   ```
