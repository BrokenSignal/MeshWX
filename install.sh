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

if [ -d "$DEST/.git" ]; then
  echo ">> updating existing checkout at $DEST"
  git -C "$DEST" pull --ff-only
else
  echo ">> cloning MeshWX to $DEST"
  git clone --depth 1 "$REPO" "$DEST"
fi

exec bash "$DEST/packaging/install-linux.sh"
