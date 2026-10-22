import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "setup.sh"


def test_setup_script_is_executable_and_valid_bash():
    assert SCRIPT.stat().st_mode & 0o111, "setup.sh must be executable"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_setup_script_help():
    out = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    for flag in ("--dev", "--install-ffmpeg", "--recreate", "--python"):
        assert flag in out.stdout


def test_setup_script_rejects_unknown_option():
    out = subprocess.run([str(SCRIPT), "--bogus"], capture_output=True, text=True)
    assert out.returncode == 2
    assert "Unknown option" in out.stderr


def test_setup_script_fails_clearly_without_uv(tmp_path):
    # PATH with only the basics so `uv` cannot be found.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "sed", "printf"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    out = subprocess.run(
        ["/bin/bash", str(SCRIPT)], capture_output=True, text=True,
        env={"PATH": str(bin_dir)},
    )
    assert out.returncode == 1
    assert "uv is not installed" in out.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_requirements_resolve_with_uv(tmp_path):
    out = subprocess.run(
        ["uv", "pip", "compile", "--quiet", "--no-header", str(ROOT / "requirements.txt")],
        capture_output=True, text=True,
        env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
    )
    offline_signs = ("network", "request failed", "tunnel error", "dns error", "connect")
    if out.returncode != 0 and any(sign in out.stderr.lower() for sign in offline_signs):
        pytest.skip("PyPI not reachable")
    assert out.returncode == 0, out.stderr
    assert "yt-dlp" in out.stdout and "pyyaml" in out.stdout.lower()
