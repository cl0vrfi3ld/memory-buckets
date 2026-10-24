#!/usr/bin/env bash
# Run the test suite on Windows Python under Wine. Manual, not part of `check`.
#
#   tests/wine.sh [pytest args...]
#
# Uses python-build-standalone's Windows CPython (what Hermes ships on native Windows)
# and installs pytest for the Windows platform with uv, so nothing runs pip inside Wine.
# Everything is cached under ${XDG_CACHE_HOME:-~/.cache}/memory-buckets/wine; delete it
# to start over. Needs x86_64 Linux, uv, and either `wine` on PATH or Nix (it falls back
# to `nix shell nixpkgs#wineWow64Packages.stable`).
#
# Wine is close to Windows but not the same: msvcrt lock failures carry errno 0 instead
# of EACCES, and os.symlink works without Developer Mode. Final checks belong on Windows.
set -euo pipefail

PBS_TAG=20261013
PBS_PY=3.14.8
PBS_SHA256=3adacae2a39dfcbebe8d95a6e87a0dee167a057ab8f822eacf38e5fa4c97f76e
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY%2B$PBS_TAG-x86_64-pc-windows-msvc-install_only.tar.gz"
PYTEST_VERSION=9.1.1  # keep in step with uv.lock
ONNXRUNTIME_VERSION=1.31.0
TOKENIZERS_VERSION=0.23.3

repo=$(realpath "$(dirname "$0")/..")
cache="${XDG_CACHE_HOME:-$HOME/.cache}/memory-buckets/wine"
python_dir="$cache/cpython-$PBS_PY-$PBS_TAG"
site="$cache/site-pytest-$PYTEST_VERSION-ort-$ONNXRUNTIME_VERSION-py${PBS_PY%.*}"
export WINEPREFIX="$cache/prefix" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" PYTHONDONTWRITEBYTECODE=1

if [ ! -x "$python_dir/python/python.exe" ]; then
  echo "fetching Windows CPython $PBS_PY ($PBS_TAG)" >&2
  mkdir -p "$cache"
  tmp=$(mktemp -d "$cache/.fetch.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/python.tar.gz" "$PBS_URL"
  echo "$PBS_SHA256  $tmp/python.tar.gz" | sha256sum -c --quiet -
  mkdir -p "$tmp/out"
  tar -xzf "$tmp/python.tar.gz" -C "$tmp/out"
  rm -rf "$python_dir"
  mv "$tmp/out" "$python_dir"
fi

if [ ! -d "$site/pytest" ]; then
  echo "installing pytest $PYTEST_VERSION, onnxruntime $ONNXRUNTIME_VERSION and tokenizers $TOKENIZERS_VERSION for Windows" >&2
  uv pip install --quiet --target "$site" --python-platform x86_64-pc-windows-msvc \
    --python-version "${PBS_PY%.*}" "pytest==$PYTEST_VERSION" "onnxruntime==$ONNXRUNTIME_VERSION" "tokenizers==$TOKENIZERS_VERSION"
fi

if command -v wine >/dev/null; then
  wine=(wine)
else
  wine=(nix shell nixpkgs#wineWow64Packages.stable -c wine)
fi

if [ ! -d "$WINEPREFIX" ]; then
  echo "creating the Wine prefix (first run only)" >&2
  "${wine[@]}" wineboot -i >/dev/null 2>&1
fi

# Wine maps / to Z:, so a Linux path becomes Z: plus the path with backslashes.
winpath() { printf 'Z:%s' "${1//\//\\}"; }
export PYTHONPATH="$(winpath "$site")"
# The Hermes probe needs Hermes's (Linux) Python, so it can't run here; model paths need translating.
unset HERMES_PYTHON
for var in MEMORY_BUCKETS_MODEL_DIR; do
  if [ -n "${!var:-}" ]; then export "$var=$(winpath "$(realpath "${!var}")")"; fi
done
cd "$repo"
"${wine[@]}" "$python_dir/python/python.exe" -m pytest -p no:cacheprovider "$@" | tr -d '\r'
