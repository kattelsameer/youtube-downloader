import json
import re
import subprocess
import sys
from pathlib import Path

import downloader

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_has_required_fields():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["name"] == "youtube-downloader"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", manifest["updated"])


def test_read_version_matches_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert downloader.read_version() == manifest["version"]


def test_read_version_unreadable_manifest(tmp_path):
    assert downloader.read_version(tmp_path / "missing.json") == "unknown"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert downloader.read_version(bad) == "unknown"


def test_changelog_has_entry_for_current_version():
    version = json.loads((ROOT / "manifest.json").read_text())["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M), \
        f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' entry"


def test_security_policy_exists():
    text = (ROOT / "SECURITY.md").read_text()
    assert "Reporting a vulnerability" in text


def test_cli_version_flag_prints_manifest_version():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    out = subprocess.run(
        [sys.executable, str(ROOT / "downloader.py"), "--version"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == f"youtube-downloader {manifest['version']}"
