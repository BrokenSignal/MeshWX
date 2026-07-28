"""Serial port discovery for Meshtastic / MeshCore nodes (any hardware).

Two device families are recognised:
  * USB-UART bridge boards (ESP32: Heltec, LILYGO) - CP210x / CH340 / CH9102,
    appearing as /dev/ttyUSB*.
  * Native-USB boards (nRF52: RAKwireless WisBlock; RP2040; ESP32-S3) - they
    appear as /dev/ttyACM* with a Nordic / Adafruit / Seeed / Espressif VID.

Candidates are then probed with the meshtastic library to confirm a live node.
Stable /dev/serial/by-id/ paths are preferred over /dev/ttyUSBx|ACMx.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass

from serial.tools import list_ports

logger = logging.getLogger("mesh_wx.serial")

# Exact (vid, pid) for the common USB-UART bridges on ESP32 mesh boards.
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60),  # Silicon Labs CP210x  (Heltec V3, many)
    (0x1A86, 0x55D4),  # WCH CH9102
    (0x1A86, 0x7523),  # WCH CH340
}
# Vendor IDs for native-USB mesh boards (match by VID; PIDs vary widely).
KNOWN_VIDS = {
    0x239A,  # Adafruit / nRF52 bootloader (RAKwireless RAK4631, etc.)
    0x2886,  # Seeed (XIAO nRF52840, T1000-E)
    0x1915,  # Nordic Semiconductor
    0x303A,  # Espressif native USB (ESP32-S3 / C3)
    0x2E8A,  # Raspberry Pi (RP2040)
    0x10C4, 0x1A86,  # the UART-bridge vendors too
}
KNOWN_DESC_HINTS = (
    "cp210", "ch9102", "ch340", "silicon labs", "usb serial", "uart",
    "nrf", "rak", "seeed", "wisblock", "meshtastic", "meshcore", "adafruit",
)


@dataclass
class PortCandidate:
    device: str
    description: str
    vid: int | None
    pid: int | None
    stable_path: str | None = None

    @property
    def preferred_path(self) -> str:
        return self.stable_path or self.device


def _by_id_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            mapping[os.path.realpath(link)] = link
        except OSError:
            continue
    return mapping


def list_all_ports() -> list[PortCandidate]:
    by_id = _by_id_map()
    out: list[PortCandidate] = []
    for p in list_ports.comports():
        out.append(PortCandidate(
            device=p.device, description=p.description or "",
            vid=p.vid, pid=p.pid,
            stable_path=by_id.get(os.path.realpath(p.device)),
        ))
    return out


def _looks_like_node(c: PortCandidate) -> bool:
    if c.vid is not None and c.pid is not None and (c.vid, c.pid) in KNOWN_USB_IDS:
        return True
    if c.vid in KNOWN_VIDS:
        return True
    dev = (c.device or "").lower()
    if "ttyacm" in dev:          # native-USB nodes (RAK/nRF52, RP2040, S3)
        return True
    desc = (c.description or "").lower()
    return any(hint in desc for hint in KNOWN_DESC_HINTS)


def candidate_ports() -> list[PortCandidate]:
    """Ports that look like a mesh node by USB identity or native-USB path."""
    return [c for c in list_all_ports() if _looks_like_node(c)]


def probe_port(device: str, timeout: float = 10.0) -> bool:
    """Try to open the port as a Meshtastic node. Returns True on success."""
    try:
        from meshtastic.serial_interface import SerialInterface
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("meshtastic import failed during probe: %s", exc)
        return False
    iface = None
    try:
        iface = SerialInterface(devPath=device)
        return getattr(iface, "myInfo", None) is not None
    except Exception as exc:
        logger.info("probe of %s failed: %s", device, exc)
        return False
    finally:
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass


def discover_port(exclude: set[str] | None = None) -> str | None:
    """Scan, probe candidates, return the preferred path of the first live node.

    Ports in ``exclude`` are skipped — used so Meshtastic auto-discovery never
    probes (and locks) a port the user has assigned to another radio, e.g. a
    MeshCore board. Probing a non-Meshtastic port blocks it for ~30s.
    """
    exclude = {e for e in (exclude or set()) if e}
    for c in candidate_ports():
        path = c.preferred_path
        if path in exclude or c.device in exclude:
            logger.info("skipping %s (assigned to another radio)", path)
            continue
        logger.info("probing candidate %s (%s)", path, c.description)
        if probe_port(path):
            logger.info("confirmed Meshtastic node at %s", path)
            return path
    return None
