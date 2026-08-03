#!/usr/bin/env bash
# One-shot native installer for Raspberry Pi / Debian / Ubuntu (no Docker).
# Installs every prerequisite, sets up a virtualenv, and registers a systemd
# service that starts on boot. Safe to re-run to update.
#
#   sudo ./packaging/install-linux.sh
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="${SUDO_USER:-$(whoami)}"

say()  { printf '\033[1;36m>> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

say "MeshWX install"
echo "   dir:  $DIR"
echo "   user: $SVC_USER"

if [ "$(id -u)" -ne 0 ]; then
  die "Please run with sudo:  sudo ./packaging/install-linux.sh"
fi

# ---- 0. self-update ----------------------------------------------------
# A stale or locally-corrupted checkout must never install old code. Force the
# tracked files to match the published version (reset --hard rewrites only
# tracked files, so the gitignored data/ database + .venv/ are left alone), then
# re-exec the (possibly updated) installer once.
if [ -z "${MESHWX_NO_SELFUPDATE:-}" ] && [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
  say "updating MeshWX to the latest version"
  git config --global --add safe.directory "$DIR" 2>/dev/null || true
  if git -C "$DIR" fetch --quiet origin 2>/dev/null \
     && git -C "$DIR" reset --hard --quiet '@{u}' 2>/dev/null; then :; \
  else warn "could not update; continuing with the local copy"; fi
  MESHWX_NO_SELFUPDATE=1 exec bash "$DIR/packaging/install-linux.sh" "$@"
fi

# ---- 1. system prerequisites -------------------------------------------
# Debian/Ubuntu split the venv module into its own package; install it up front
# so users never hit "python3-venv not available" or a broken ensurepip.
if command -v apt-get >/dev/null 2>&1; then
  say "installing system packages (python3, venv, pip, git)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git
elif command -v dnf >/dev/null 2>&1; then
  say "installing system packages via dnf"
  dnf install -y python3 python3-pip git
elif command -v pacman >/dev/null 2>&1; then
  say "installing system packages via pacman"
  pacman -Sy --needed --noconfirm python python-pip git
else
  warn "Unknown package manager. Ensure python3 (>=3.11), the venv module, pip and git are installed."
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found after install."
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "using Python $PYVER"

# ---- 2. virtualenv + dependencies --------------------------------------
say "creating virtualenv"
"$PY" -m venv "$DIR/.venv" || die "could not create venv (is python3-venv installed?)"
"$DIR/.venv/bin/python" -m pip install --upgrade --quiet pip
say "installing MeshWX dependencies (this can take a couple of minutes)"
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt" \
  || die "dependency install failed. Re-run, or report the output at the project's Issues page."

# ---- 3. serial access ---------------------------------------------------
say "granting serial access: adding $SVC_USER to the 'dialout' group"
usermod -aG dialout "$SVC_USER" || warn "could not add $SVC_USER to dialout; add it manually."

# ---- 4. data dir + systemd service -------------------------------------
mkdir -p "$DIR/data"
chown -R "$SVC_USER" "$DIR/data" "$DIR/.venv"

say "installing systemd service"
sed -e "s#__USER__#$SVC_USER#g" -e "s#__DIR__#$DIR#g" \
  "$DIR/packaging/mesh-wx.service" > /etc/systemd/system/mesh-wx.service
systemctl daemon-reload
systemctl enable --now mesh-wx.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
say "done. MeshWX is running at  http://${IP:-<this-host>}:8110"
echo "   logs:    journalctl -u mesh-wx -f"
echo "   restart: sudo systemctl restart mesh-wx"
echo "   status:  systemctl status mesh-wx"
if ! id -nG "$SVC_USER" | grep -qw dialout; then
  warn "You were just added to 'dialout' for serial access. Log out/in (or reboot)"
  warn "so the service can open the radio's USB port."
fi
