"""Command-line interface."""

from __future__ import annotations

import argparse

from .version import read_manifest, read_version


def build_arg_parser() -> argparse.ArgumentParser:
    manifest = read_manifest()
    parser = argparse.ArgumentParser(
        prog="downloader.py",
        description="Download YouTube audio/video from a multi-job YAML config, "
                    "only fetching what is new since the last run.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"{manifest.get('name', 'youtube-downloader')} {read_version()}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0

