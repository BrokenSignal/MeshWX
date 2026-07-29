"""Bootstrap configuration from environment variables.

Only the values needed to *start* the app live here (HTTP port, db path).
Everything else is stored in the database and editable in the UI.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIRNAME = "MeshWX"


def default_data_dir() -> Path:
    """Per-OS location for the database and other runtime state.

    Chosen so the app runs unprivileged out-of-the-box on every platform:
      * Docker/Linux containers .......... /data   (if it exists and is writable)
      * Windows .......................... %LOCALAPPDATA%\\MeshWX
      * macOS ............................ ~/Library/Application Support/MeshWX
      * Linux/Raspberry Pi (native) ...... $XDG_DATA_HOME/mesh-wx  (~/.local/share/mesh-wx)
    """
    # Honour the container convention when /data is mounted.
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return Path("/data")

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIRNAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRNAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "mesh-wx"


@dataclass(frozen=True)
class BootstrapConfig:
    http_host: str
    http_port: int
    db_path: str


def load_bootstrap() -> BootstrapConfig:
    db_path = os.environ.get("MESH_WX_DB")
    if not db_path:
        data_dir = default_data_dir()
        db_path = str(data_dir / "mesh-wx.db")
    # Make sure the parent directory exists so SQLite can create the file.
    parent = Path(db_path).expanduser().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return BootstrapConfig(
        http_host=os.environ.get("MESH_WX_HOST", "0.0.0.0"),
        http_port=int(os.environ.get("MESH_WX_PORT", "8000")),
        db_path=str(Path(db_path).expanduser()),
    )


# Default settings seeded into the db on first run. Every one of these is
# editable in the UI afterwards; env vars never override stored settings.
DEFAULT_SETTINGS: dict = {
    "zones": "SCZ050",
    "poll_interval": 120,
    "nws_contact": "mesh-wx (change-me@example.com)",
    "channel_index": 0,
    "serial_port": "",
    "meshtastic_enabled": True,
    "meshtastic_conn": "serial",
    "meshtastic_host": "",
    "meshtastic_repeat": 2,
    "meshcore_enabled": False,
    "meshcore_conn": "serial",
    "meshcore_port": "",
    "meshcore_host": "",
    "meshcore_channel": 0,
    "meshcore_repeat": 2,
    "dry_run": True,
    "test_channel": 1,   # tests + manual sends use this channel (keep off the live alert channel 0)
    "display_timezone": "America/New_York",
    # Filter rules (editable). An alert is INCLUDED when its event is in
    # filter_include_exact OR ends with any suffix in filter_include_suffix,
    # UNLESS the event is in filter_exclude_exact.
    "filter_include_exact": ["Tornado Watch"],
    "filter_include_suffix": ["Warning"],
    "filter_exclude_exact": [],
}

POLL_INTERVAL_MIN = 60
MAX_PAYLOAD_BYTES = 195
BURST_GAP_SECONDS = 30
REPEAT_GAP_SECONDS = 5   # gap between repeated copies of the same alert
QUEUE_MAX = 20
STATE_EXPIRY_HOURS = 48
