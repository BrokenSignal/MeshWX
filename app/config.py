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
    "meshtastic_test_channel": 1,
    "meshcore_enabled": False,
    "meshcore_conn": "serial",
    "meshcore_port": "",
    "meshcore_host": "",
    "meshcore_channel": 0,
    "meshcore_repeat": 2,
    "meshcore_test_channel": 1,
    "dry_run": True,
    "test_channel": 1,   # tests + manual sends use this channel (keep off the live alert channel 0)
    "display_timezone": "",   # blank = use this computer's local time zone
    # Filter rules (editable). An alert is INCLUDED when its event is in
    # filter_include_exact OR ends with any suffix in filter_include_suffix,
    # UNLESS the event is in filter_exclude_exact.
    "filter_include_exact": ["Tornado Watch"],
    "filter_include_suffix": ["Warning"],
    "filter_exclude_exact": [],
}

POLL_INTERVAL_MIN = 60
# Hard ceiling on a single poll cycle. The NWS fetch is already bounded (30s
# timeout x a few retries), so exceeding this means something hung (DB lock,
# wedged await, a bug). The watchdog aborts the poll so the loop always recovers.
POLL_HARD_TIMEOUT = 180
MAX_PAYLOAD_BYTES = 195
BURST_GAP_SECONDS = 30
REPEAT_GAP_SECONDS = 5   # gap between repeated copies of the same alert
QUEUE_MAX = 20
STATE_EXPIRY_HOURS = 48

# IPAWS (FEMA) public feed. Separate, experimental pipeline (VM only for now):
# re-broadcasts non-weather alerts on the TEST channel. FEMA asks for polling no
# more often than every 2 minutes -- enforced as a hard floor.
IPAWS_BASE_URL = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest"
IPAWS_PATH = "public"                     # public feed (weather senders filtered out in code)
IPAWS_POLL_SECONDS = 120                  # >= 120: never poll FEMA more often than every 2 min
IPAWS_POLL_FLOOR = 120                    # hard minimum, do not go below
IPAWS_LOOKBACK_SECONDS = 600              # first poll window on startup
# Senders whose alerts we DROP (NWS weather already goes out via the NWS pipeline).
IPAWS_WEATHER_SENDER_HINTS = ("noaa.gov", "nws")
# IPAWS DMOPEN "proficiency demonstration" messages arrive as status=Actual but
# are NOT real emergencies (COGs sending them to prove their system works).
# Drop anything whose event/identifier matches these (case-insensitive substrings).
IPAWS_DEMO_PATTERNS = ("live_data", "proficiency", "demonstration", "dmopen",
                       "external (", "external_(")

# IPAWS non-weather alert categories the user can choose to broadcast (like the
# NOAA event filter). Each is (key, label, keyword-substrings matched against the
# alert's event, case-insensitive). "other" is the catch-all for anything else.
IPAWS_EVENT_TYPES = [
    ("amber",     "AMBER / Child abduction",   ["amber", "child abduction"]),
    ("civil",     "Civil danger / emergency",  ["civil danger", "civil emergency"]),
    ("evacuation","Evacuation",                ["evacuation", "evacuate"]),
    ("shelter",   "Shelter in place",          ["shelter in place", "shelter-in-place"]),
    ("fire",      "Fire",                       ["fire warning", "wildfire", "fire"]),
    ("hazmat",    "Hazardous materials",       ["hazardous material", "hazmat", "chemical"]),
    ("law",       "Law enforcement",           ["law enforcement", "blue alert"]),
    ("local",     "Local area emergency",      ["local area emergency"]),
    ("outage",    "911 / utility outage",      ["911", "telephone outage", "utility"]),
    ("nuclear",   "Nuclear / radiological",    ["nuclear", "radiological", "radiation"]),
    ("water",     "Water / boil-water",        ["boil water", "boil-water", "water advisory"]),
    ("other",     "Other public safety",       []),
]
