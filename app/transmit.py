"""Multi-protocol transmit layer - Meshtastic + MeshCore, side by side.

Each protocol is an independent transport with its own enable flag and
connection (serial or TCP). TransmitManager fans every outbound message out to
ALL enabled transports, paces bursts, and manages per-transport reconnects.
Dry-run lives in the poller; manual sends bypass pacing. Backends are imported
lazily so a missing optional dependency (e.g. meshcore) never breaks startup.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from .config import BURST_GAP_SECONDS, QUEUE_MAX

logger = logging.getLogger("mesh_wx.tx")


class Transmitter(abc.ABC):
    label = "?"

    @abc.abstractmethod
    async def connect(self) -> None: ...
    @abc.abstractmethod
    async def send_text(self, text: str, channel: int) -> None: ...
    @abc.abstractmethod
    async def close(self) -> None: ...
    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...


class MeshtasticTransmitter(Transmitter):
    label = "Meshtastic"

    def __init__(self, conn: str, port: str = "", host: str = ""):
        self.conn, self.port, self.host = conn, port, host
        self._iface = None

    async def connect(self) -> None:
        if self._iface is not None:
            await self.close()

        def _open():
            if self.conn == "tcp":
                from meshtastic.tcp_interface import TCPInterface
                return TCPInterface(hostname=self.host)
            from meshtastic.serial_interface import SerialInterface
            return SerialInterface(devPath=self.port)

        self._iface = await asyncio.get_event_loop().run_in_executor(None, _open)

    async def send_text(self, text: str, channel: int) -> None:
        if self._iface is None:
            raise RuntimeError("not connected")
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._iface.sendText(text, channelIndex=channel))

    async def close(self) -> None:
        if self._iface is not None:
            iface, self._iface = self._iface, None
            try:
                await asyncio.get_event_loop().run_in_executor(None, iface.close)
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._iface is not None


class MeshCoreTransmitter(Transmitter):
    label = "MeshCore"

    def __init__(self, conn: str, port: str = "", host: str = "", baud: int = 115200):
        self.conn, self.port, self.host, self.baud = conn, port, host, baud
        self._mc = None

    async def connect(self) -> None:
        from meshcore import MeshCore  # lazy: optional dependency
        if self._mc is not None:
            await self.close()
        # default_timeout: cap how long we wait for the device's "OK" confirmation
        #   (channel broadcasts have no real ACK, so a long wait just stalls sends).
        # auto_reconnect: let the library recover a dropped USB/TCP link on its own.
        if self.conn == "tcp":
            h, _, p = self.host.partition(":")
            self._mc = await MeshCore.create_tcp(
                h, int(p or 4000), default_timeout=6.0, auto_reconnect=True)
        else:
            self._mc = await MeshCore.create_serial(
                self.port, self.baud, default_timeout=6.0, auto_reconnect=True)
        if self._mc is None:
            where = self.host if self.conn == "tcp" else self.port
            raise RuntimeError("no response from MeshCore node on %s" % (where or "(unset)"))

    async def send_text(self, text: str, channel: int) -> None:
        if self._mc is None:
            raise RuntimeError("not connected")
        from meshcore import EventType
        res = await self._mc.commands.send_chan_msg(channel, text)
        if getattr(res, "type", None) == EventType.ERROR:
            payload = getattr(res, "payload", {}) or {}
            reason = payload.get("reason") if isinstance(payload, dict) else None
            # A missing confirmation frame is NOT a delivery failure: a channel
            # broadcast has no ACK, and the device likely still transmitted it
            # (the meshcore lib says as much). Treat as best-effort success and
            # KEEP the link up - otherwise every unconfirmed send tears the radio
            # down and it flaps offline.
            if reason in ("no_event_received", "timeout"):
                logger.warning(
                    "meshcore: channel send unconfirmed (%s); treating as sent", reason)
                return
            raise RuntimeError("meshcore error: %s" % payload)

    async def close(self) -> None:
        if self._mc is not None:
            mc, self._mc = self._mc, None
            try:
                await mc.disconnect()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._mc is not None


@dataclass
class Transport:
    name: str                     # "meshtastic" | "meshcore"
    label: str
    enabled: bool
    conn: str                     # "serial" | "tcp"
    channel: int
    target: str                   # serial path or host - display + "configured?" check
    make: object                  # callable() -> Transmitter
    tx: Transmitter | None = None
    connected: bool = False
    error: str = ""


@dataclass
class QueueItem:
    text: str


def _build_transports(db) -> dict:
    def g(k, d=None):
        return db.get_setting(k, d)

    mt_conn = g("meshtastic_conn", "serial") or "serial"
    mt = Transport(
        name="meshtastic", label="Meshtastic",
        enabled=bool(g("meshtastic_enabled", True)),
        conn=mt_conn, channel=int(g("channel_index", 0) or 0),
        target=(g("meshtastic_host", "") if mt_conn == "tcp" else g("serial_port", "")) or "",
        make=lambda: MeshtasticTransmitter(mt_conn, g("serial_port", "") or "", g("meshtastic_host", "") or ""),
    )
    mc_conn = g("meshcore_conn", "serial") or "serial"
    mc = Transport(
        name="meshcore", label="MeshCore",
        enabled=bool(g("meshcore_enabled", False)),
        conn=mc_conn, channel=int(g("meshcore_channel", 0) or 0),
        target=(g("meshcore_host", "") if mc_conn == "tcp" else g("meshcore_port", "")) or "",
        make=lambda: MeshCoreTransmitter(mc_conn, g("meshcore_port", "") or "", g("meshcore_host", "") or ""),
    )
    return {"meshtastic": mt, "meshcore": mc}


class TransmitManager:
    """Serializes node access, paces bursts, fans out to all enabled transports."""

    def __init__(self, db):
        self._db = db
        self._transports = _build_transports(db)
        self._queue: deque[QueueItem] = deque(maxlen=QUEUE_MAX)
        self._queue_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._reconnect_delay = 2.0
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._stopped = False
        self._worker_task = asyncio.create_task(self._worker(), name="tx-worker")

    async def stop(self) -> None:
        self._stopped = True
        self._queue_event.set()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for t in self._transports.values():
            if t.tx:
                await t.tx.close()

    async def reconfigure(self) -> None:
        """Rebuild transports from settings and connect the enabled ones.
        Called at startup and after a settings save."""
        async with self._lock:
            for t in self._transports.values():
                if t.tx:
                    try:
                        await t.tx.close()
                    except Exception:
                        pass
            self._transports = _build_transports(self._db)
            targets = [t for t in self._transports.values() if t.enabled and t.target]
        for t in targets:
            await self._ensure(t)

    # ---- status / compat ------------------------------------------------
    @property
    def connected(self) -> bool:
        return any(t.connected for t in self._transports.values() if t.enabled)

    @property
    def port(self) -> str | None:
        return self._transports["meshtastic"].target or None

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def last_error(self) -> str:
        for t in self._transports.values():
            if t.enabled and t.error:
                return "%s: %s" % (t.label, t.error)
        return ""

    def status(self) -> list[dict]:
        return [
            {"name": t.name, "label": t.label, "enabled": t.enabled,
             "conn": t.conn, "connected": t.connected, "target": t.target,
             "channel": t.channel, "error": t.error}
            for t in self._transports.values()
        ]

    async def set_port(self, port: str) -> None:
        """Back-compat: set the Meshtastic serial port, then reconnect."""
        self._db.set_setting("serial_port", port or "")
        await self.reconfigure()

    # ---- connection -----------------------------------------------------
    async def _ensure(self, t: Transport) -> bool:
        if t.connected and t.tx is not None:
            return True
        if not t.enabled:
            return False
        if not t.target:
            t.error = "no connection configured"
            return False
        try:
            tx = t.make()
            await tx.connect()
            t.tx, t.connected, t.error = tx, True, ""
            self._reconnect_delay = 2.0
            self._db.add_event("INFO", "%s connected (%s)" % (t.label, t.target))
            logger.info("%s connected at %s", t.name, t.target)
            return True
        except Exception as exc:
            t.tx, t.connected = None, False
            t.error = "connect failed: %s" % exc
            self._db.add_error(t.name, t.error)
            logger.warning("%s connect failed: %s", t.name, exc)
            return False

    # ---- sending --------------------------------------------------------
    def enqueue(self, text: str, channel: int | None = None) -> bool:
        """Queue an automated transmission (fanned out to all enabled transports).
        The channel arg is ignored - each transport uses its own configured channel."""
        dropped = len(self._queue) == self._queue.maxlen
        self._queue.append(QueueItem(text=text))
        self._queue_event.set()
        if dropped:
            logger.warning("transmit queue full; dropped oldest")
            self._db.add_event("WARN", "transmit queue full; dropped oldest message")
        return not dropped

    async def _send_all(self, text: str, manual: bool) -> bool:
        any_ok = False
        blen = len(text.encode())
        async with self._lock:
            for t in self._transports.values():
                if not t.enabled:
                    continue
                if not await self._ensure(t):
                    self._db.add_transmit_log(t.channel, blen, False, text, manual,
                                              error=t.error, transport=t.name)
                    continue
                try:
                    await t.tx.send_text(text, t.channel)
                    self._db.add_transmit_log(t.channel, blen, True, text, manual,
                                              transport=t.name)
                    any_ok = True
                    logger.info("transmitted via %s", t.name,
                                extra={"channel": t.channel, "bytes": blen,
                                       "action": "manual" if manual else "auto"})
                except Exception as exc:
                    t.error = "send failed: %s" % exc
                    t.connected = False
                    try:
                        await t.tx.close()
                    except Exception:
                        pass
                    t.tx = None
                    self._db.add_transmit_log(t.channel, blen, False, text, manual,
                                              error=str(exc), transport=t.name)
                    self._db.add_error(t.name, t.error)
                    logger.warning("%s send failed: %s", t.name, exc)
        return any_ok

    async def send_manual(self, text: str, channel: int | None = None) -> bool:
        return await self._send_all(text, manual=True)

    async def send_to(self, name: str, text: str) -> tuple[bool, str]:
        """Key up a single named radio (bench testing). Returns (ok, error)."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            if not await self._ensure(t):
                self._db.add_transmit_log(t.channel, blen, False, text, True,
                                          error=t.error, transport=t.name)
                return False, t.error or "not connected"
            try:
                await t.tx.send_text(text, t.channel)
                self._db.add_transmit_log(t.channel, blen, True, text, True,
                                          transport=t.name)
                logger.info("test transmitted via %s", t.name)
                return True, ""
            except Exception as exc:
                t.error = "send failed: %s" % exc
                t.connected = False
                try:
                    await t.tx.close()
                except Exception:
                    pass
                t.tx = None
                self._db.add_transmit_log(t.channel, blen, False, text, True,
                                          error=str(exc), transport=t.name)
                self._db.add_error(t.name, t.error)
                logger.warning("%s test send failed: %s", t.name, exc)
                return False, str(exc)

    async def _worker(self) -> None:
        first = True
        while not self._stopped:
            if not self._queue:
                self._queue_event.clear()
                try:
                    await self._queue_event.wait()
                except asyncio.CancelledError:
                    return
                first = True
                continue
            if not first:
                try:
                    await asyncio.sleep(BURST_GAP_SECONDS)
                except asyncio.CancelledError:
                    return
            item = self._queue.popleft()
            ok = await self._send_all(item.text, manual=False)
            first = False
            if not ok:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)
