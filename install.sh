#!/usr/bin/env bash
# MeshWX one-line installer for Debian / Ubuntu / Raspberry Pi:
#
#   curl -fsSL https://raw.githubusercontent.com/BrokenSignal/MeshWX/main/install.sh | sudo bash
#
# Installs git, clones the repo to /opt/MeshWX (override with MESHWX_DIR=...),
# and runs the full native installer.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  echo "Run with sudo:  curl -fsSL <url>/install.sh | sudo bash" >&2; exit 1; }

DEST="${MESHWX_DIR:-/opt/MeshWX}"
REPO="https://github.com/BrokenSignal/MeshWX.git"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq git
fi

# Force $DEST to EXACTLY match the latest published code. `git reset --hard`
# only rewrites tracked files, so it repairs any local corruption (edited
# requirements, half-applied changes) while leaving the gitignored data/ (the
# database + settings) and .venv/ untouched. Re-clone only if there is no usable
# checkout, preserving data/ across the re-clone.
sync_to_latest() {
  git config --global --add safe.directory "$DEST" 2>/dev/null || true
  if [ -d "$DEST/.git" ] && git -C "$DEST" rev-parse --git-dir >/dev/null 2>&1 \
     && git -C "$DEST" fetch --depth 1 --quiet origin main 2>/dev/null \
     && git -C "$DEST" reset --hard --quiet FETCH_HEAD 2>/dev/null; then
    echo ">> synced $DEST to the latest MeshWX"
    return 0
  fi
  echo ">> setting up a clean checkout at $DEST"
  local keep=""
  if [ -d "$DEST/data" ]; then keep="$(mktemp -d)"; mv "$DEST/data" "$keep/"; fi
  rm -rf "$DEST"
  git clone --depth 1 "$REPO" "$DEST"
  if [ -n "$keep" ]; then rm -rf "$DEST/data"; mv "$keep/data" "$DEST/data"; rmdir "$keep"; fi
}
sync_to_latest

# We just synced; skip install-linux.sh's own self-update to avoid a double pull.
MESHWX_NO_SELFUPDATE=1 exec bash "$DEST/packaging/install-linux.sh"
