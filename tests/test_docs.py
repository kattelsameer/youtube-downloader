"""Keep the README in step with the code."""
from pathlib import Path

from youtube_downloader import cli
from youtube_downloader.config import BUILTIN_DEFAULTS, GLOBAL_ONLY_KEYS, JOB_ONLY_KEYS

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def test_every_setting_is_documented():
    missing = [k for k in [*BUILTIN_DEFAULTS, *GLOBAL_ONLY_KEYS, *JOB_ONLY_KEYS] if f"`{k}`" not in README]
    assert missing == []


def test_every_cli_flag_is_documented():
    flags = [opt for action in cli.build_arg_parser()._actions for opt in action.option_strings
             if opt.startswith("--") and opt != "--help"]
    assert [f for f in flags if f"`{f}" not in README] == []


def test_readme_covers_required_topics():
    for heading in ("## Setup", "### ffmpeg", "## Config reference", "## The metadata file",
                    "## Incremental sync rules", "## Command line", "## Contributing"):
        assert heading in README


def test_contributing_guide_covers_the_workflow():
    guide = (Path(__file__).resolve().parent.parent / "CONTRIBUTING.md").read_text()
    for heading in ("## Branches", "## Making a change", "## Versioning", "## Commits",
                    "## Pull requests"):
        assert heading in guide
    for branch in ("`develop`", "`master`", "`feature/<short-name>`", "`fix/<short-name>`"):
        assert branch in guide
    assert "## Releases" not in guide  # releases are a maintainer task
