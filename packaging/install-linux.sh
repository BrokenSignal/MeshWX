#!/usr/bin/env bash
# Native install for Raspberry Pi / Linux (no Docker).
# Creates a virtualenv, installs MeshWX, and registers a systemd service.
#
#   sudo ./packaging/install-linux.sh
#
# Re-run any time to update; it is idempotent.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="${SUDO_USER:-$(whoami)}"
PY="${PYTHON:-python3}"

echo ">> MeshWX install"
echo "   dir:  $DIR"
echo "   user: $SVC_USER"

if [ "$(id -u)" -ne 0 ]; then
  echo "!! Please run with sudo (needed to install the systemd service)." >&2
  exit 1
fi

command -v "$PY" >/dev/null || { echo "!! python3 not found; install it first (sudo apt install python3 python3-venv)"; exit 1; }

echo ">> creating virtualenv"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

echo ">> serial access: adding $SVC_USER to the 'dialout' group"
usermod -aG dialout "$SVC_USER" || true

mkdir -p "$DIR/data"
chown -R "$SVC_USER" "$DIR/data"

echo ">> installing systemd service"
sed -e "s#__USER__#$SVC_USER#g" -e "s#__DIR__#$DIR#g" \
  "$DIR/packaging/mesh-wx.service" > /etc/systemd/system/mesh-wx.service
systemctl daemon-reload
systemctl enable --now mesh-wx.service

echo ""
echo ">> done. MeshWX is running on http://$(hostname -I | awk '{print $1}'):8110"
echo "   logs:    journalctl -u mesh-wx -f"
echo "   restart: sudo systemctl restart mesh-wx"
echo "   NOTE: if this was the first time adding you to 'dialout', log out/in"
echo "         (or reboot) so the service can open the serial port."
