#!/usr/bin/env bash
# Set up the environment for downloader.py using uv.
#
#   ./setup.sh                  create .venv and install runtime dependencies
#   ./setup.sh --dev            also install test dependencies (pytest)
#   ./setup.sh --install-ffmpeg install ffmpeg with brew/apt/dnf if it is missing
#   ./setup.sh --recreate       delete and rebuild .venv
#   ./setup.sh --python 3.13    pick the Python version uv should use
#
# Exits non-zero if something required (uv, Python deps, ffmpeg) is missing.
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON_SPEC=">=3.10"
DEV=0
INSTALL_FFMPEG=0
RECREATE=0

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dev) DEV=1 ;;
    --install-ffmpeg) INSTALL_FFMPEG=1 ;;
    --recreate) RECREATE=1 ;;
    --python) shift; PYTHON_SPEC="${1:?--python needs a version}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

info() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# 1. uv
if ! have uv; then
  fail "uv is not installed. Install it with one of:
    brew install uv
    pipx install uv
    curl -LsSf https://astral.sh/uv/install.sh | sh
  then re-run ./setup.sh"
fi
info "Using $(uv --version)"

# 2. Virtualenv (uv downloads a matching Python if none is installed)
if [ "$RECREATE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
  info "Removing existing $VENV_DIR"
  rm -rf "$VENV_DIR"
fi
if [ -x "$VENV_DIR/bin/python" ]; then
  info "Reusing $VENV_DIR ($("$VENV_DIR/bin/python" --version))"
else
  info "Creating $VENV_DIR with Python $PYTHON_SPEC"
  uv venv --python "$PYTHON_SPEC" "$VENV_DIR"
fi

# 3. Python dependencies
REQ="requirements.txt"
[ "$DEV" -eq 1 ] && REQ="requirements-dev.txt"
info "Installing dependencies from $REQ"
uv pip install --python "$VENV_DIR/bin/python" -r "$REQ"

# 4. ffmpeg / ffprobe (mp3 extraction, mp4 merging, file checks)
install_ffmpeg() {
  if have brew; then brew install ffmpeg
  elif have apt-get; then sudo apt-get update && sudo apt-get install -y ffmpeg
  elif have dnf; then sudo dnf install -y ffmpeg
  else return 1
  fi
}
MISSING=0
if have ffmpeg && have ffprobe; then
  info "Found $(ffmpeg -version | head -n 1 | cut -d' ' -f1-3)"
else
  if [ "$INSTALL_FFMPEG" -eq 0 ] && [ -t 0 ]; then
    read -r -p "ffmpeg/ffprobe not found. Install now? [y/N] " answer
    case "$answer" in [yY]*) INSTALL_FFMPEG=1 ;; esac
  fi
  if [ "$INSTALL_FFMPEG" -eq 1 ]; then
    info "Installing ffmpeg"
    install_ffmpeg || warn "No supported package manager found (brew, apt-get, dnf)."
  fi
  if ! (have ffmpeg && have ffprobe); then
    warn "ffmpeg/ffprobe not found. Install it (macOS: brew install ffmpeg," \
         "Debian/Ubuntu: sudo apt-get install ffmpeg, Windows: winget install ffmpeg)."
    MISSING=1
  fi
fi

# 5. JavaScript runtime (yt-dlp needs one to solve YouTube's JS challenges)
JS_RUNTIME=""
for rt in deno node bun; do
  if have "$rt"; then JS_RUNTIME="$rt"; break; fi
done
if [ -n "$JS_RUNTIME" ]; then
  info "Found JavaScript runtime: $JS_RUNTIME ($("$JS_RUNTIME" --version 2>/dev/null | head -n 1))"
else
  warn "No JavaScript runtime (deno, node or bun) found. YouTube downloads may miss" \
       "formats or fail. Recommended: brew install deno"
fi

# 6. Smoke test
"$VENV_DIR/bin/python" -c "import yt_dlp, yaml; print('==> yt-dlp', yt_dlp.version.__version__)"
"$VENV_DIR/bin/python" downloader.py --version

if [ "$MISSING" -ne 0 ]; then
  fail "Setup incomplete: install ffmpeg, then re-run ./setup.sh"
fi

cat <<EOF
==> Setup complete. Next steps:
    source $VENV_DIR/bin/activate
    python downloader.py --config config.yaml --list
EOF
