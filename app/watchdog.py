"""Liveness watchdog.

`restart: unless-stopped` (Docker) and `Restart=on-failure` (systemd) relaunch a
process that CRASHES, but not one that is wedged-but-alive: a deadlock or a sync
call blocking the asyncio event loop leaves the process "running" while it quietly
stops polling NOAA and stops transmitting. This watchdog catches that.

A plain OS thread (independent of the event loop, so it keeps running even when
the loop is blocked) checks a heartbeat that an event-loop task refreshes. If the
loop stops refreshing it for `stall_seconds`, the loop is wedged, so we force-exit
non-zero and let the restart policy bring up a healthy instance.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("mesh_wx.watchdog")


class Liveness:
    def __init__(self, stall_seconds: float = 90.0):
        self._stall = stall_seconds
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """Called from the event loop to prove it is still making progress."""
        self._last = time.monotonic()

    def _run(self) -> None:
        interval = min(15.0, self._stall / 3)
        while not self._stop.wait(interval):
            idle = time.monotonic() - self._last
            if idle > self._stall:
                logger.critical(
                    "event loop stalled %.0fs (> %.0fs); forcing restart", idle, self._stall)
                os._exit(1)  # non-zero -> restart:unless-stopped / systemd Restart relaunch

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="liveness", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Called on graceful shutdown so we never force-exit during a clean stop.
        self._stop.set()
