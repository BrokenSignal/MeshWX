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

from .config import BURST_GAP_SECONDS, REPEAT_GAP_SECONDS, QUEUE_MAX

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

    async def read_channels(self) -> list:
        """Return the channels configured on the device: [{index, name}]. Optional."""
        return []


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
                h, _, p = (self.host or "").partition(":")
                if p:  # explicit host:port
                    return TCPInterface(hostname=h, portNumber=int(p))
                return TCPInterface(hostname=h)
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

    async def read_channels(self) -> list:
        if self._iface is None:
            raise RuntimeError("not connected")

        def _read():
            out = []
            node = getattr(self._iface, "localNode", None)
            for ch in (getattr(node, "channels", None) or []):
                role = int(getattr(ch, "role", 0))  # 0 DISABLED, 1 PRIMARY, 2 SECONDARY
                if role == 0:
                    continue
                name = ""
                if getattr(ch, "settings", None) is not None:
                    name = ch.settings.name or ""
                if not name:
                    name = "LongFast" if role == 1 else ("channel %d" % ch.index)
                out.append({"index": int(ch.index), "name": name})
            return out

        return await asyncio.get_event_loop().run_in_executor(None, _read)

    async def read_info(self) -> dict:
        if self._iface is None:
            return {}

        def _read():
            info = {}
            try:
                ni = self._iface.getMyNodeInfo() or {}
                hw = (ni.get("user") or {}).get("hwModel")
                if hw:
                    info["model"] = str(hw)
            except Exception:
                pass
            try:
                meta = getattr(self._iface, "metadata", None)
                fw = getattr(meta, "firmware_version", "") if meta is not None else ""
                if fw:
                    info["firmware"] = str(fw)
            except Exception:
                pass
            return info

        return await asyncio.get_event_loop().run_in_executor(None, _read)


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

    async def read_channels(self) -> list:
        if self._mc is None:
            raise RuntimeError("not connected")
        from meshcore import EventType
        out = []
        for idx in range(0, 8):
            res = await self._mc.commands.get_channel(idx)
            if getattr(res, "type", None) != EventType.CHANNEL_INFO:
                break  # device ran out of channel slots
            p = getattr(res, "payload", {}) or {}
            name = (p.get("channel_name") or "").strip()
            if not name:
                continue  # empty/unconfigured slot
            out.append({"index": int(p.get("channel_idx", idx)), "name": name})
        return out

    async def read_info(self) -> dict:
        if self._mc is None:
            return {}
        from meshcore import EventType
        try:
            res = await self._mc.commands.send_device_query()
        except Exception:
            return {}
        if getattr(res, "type", None) != EventType.DEVICE_INFO:
            return {}
        p = getattr(res, "payload", {}) or {}
        return {"model": (p.get("model") or "").strip(),
                "firmware": (p.get("ver") or "").strip()}


def _fmt_model(info: dict) -> str:
    """Human label for a radio from its info dict, e.g. 'Heltec V3 (fw 2.5.9)'."""
    model = (info.get("model") or "").replace("_", " ").strip()
    fw = (info.get("firmware") or "").strip()
    if model and fw:
        return "%s (fw %s)" % (model, fw)
    return model or (("fw %s" % fw) if fw else "")


@dataclass
class Transport:
    name: str                     # "meshtastic" | "meshcore"
    label: str
    enabled: bool
    conn: str                     # "serial" | "tcp"
    channel: int
    target: str                   # serial path or host - display + "configured?" check
    make: object                  # callable() -> Transmitter
    repeat: int = 1               # send each alert this many times (LoRa has no ACK)
    test_channel: int = 1         # channel used for Troubleshoot tests only
    tx: Transmitter | None = None
    connected: bool = False
    error: str = ""


@dataclass
class QueueItem:
    text: str


def _build_transports(db) -> dict:
    def g(k, d=None):
        return db.get_setting(k, d)

    def num(k, d=0):
        try:
            return int(g(k, d) or d)
        except (TypeError, ValueError):
            return d

    def rep(k):
        return max(1, min(5, num(k, 2)))

    mt_conn = g("meshtastic_conn", "serial") or "serial"
    mt = Transport(
        name="meshtastic", label="Meshtastic",
        enabled=bool(g("meshtastic_enabled", True)),
        conn=mt_conn, channel=num("channel_index", 0),
        target=(g("meshtastic_host", "") if mt_conn == "tcp" else g("serial_port", "")) or "",
        make=lambda: MeshtasticTransmitter(mt_conn, g("serial_port", "") or "", g("meshtastic_host", "") or ""),
        repeat=rep("meshtastic_repeat"), test_channel=num("meshtastic_test_channel", 1),
    )
    mc_conn = g("meshcore_conn", "serial") or "serial"
    mc = Transport(
        name="meshcore", label="MeshCore",
        enabled=bool(g("meshcore_enabled", False)),
        conn=mc_conn, channel=num("meshcore_channel", 0),
        target=(g("meshcore_host", "") if mc_conn == "tcp" else g("meshcore_port", "")) or "",
        make=lambda: MeshCoreTransmitter(mc_conn, g("meshcore_port", "") or "", g("meshcore_host", "") or ""),
        repeat=rep("meshcore_repeat"), test_channel=num("meshcore_test_channel", 1),
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

    async def _try_send(self, t: Transport, text: str, ch: int) -> tuple[bool, str]:
        """Send once; if the link is dead (e.g. an idle TCP socket giving a
        Broken pipe), reconnect and retry once. Returns (ok, error)."""
        last = "not connected"
        for attempt in (1, 2):
            if t.tx is None or not t.connected:
                if not await self._ensure(t):
                    last = t.error or "not connected"
                    continue
            try:
                await t.tx.send_text(text, ch)
                if attempt > 1:
                    self._db.add_event("INFO", "%s recovered a dropped link and sent" % t.label)
                return True, ""
            except Exception as exc:
                last = "send failed: %s" % exc
                t.error, t.connected = last, False
                try:
                    await t.tx.close()
                except Exception:
                    pass
                t.tx = None
                logger.warning("%s send failed (attempt %d/2): %s", t.name, attempt, exc)
        self._db.add_error(t.name, last)   # only a real error if the retry also failed
        return False, last

    async def _send_all(self, text: str, manual: bool, on_test: bool | None = None) -> bool:
        any_ok = False
        blen = len(text.encode())
        # `on_test` picks the channel (test vs live); `manual` only tags the log
        # (auto vs manual). Automated alerts AND composed manual sends both go on
        # each radio's LIVE channel; only the Troubleshoot test uses the test channel.
        use_test = manual if on_test is None else on_test
        async with self._lock:
            # LoRa broadcasts are unacked, so send t.repeat times; each send
            # reconnects+retries once on a dead link.
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.test_channel if use_test else t.channel
                for i in range(max(1, t.repeat)):
                    if i > 0:
                        await asyncio.sleep(REPEAT_GAP_SECONDS)
                    ok, err = await self._try_send(t, text, ch)
                    self._db.add_transmit_log(ch, blen, ok, text, manual,
                                              error=("" if ok else err), transport=t.name)
                    if ok:
                        any_ok = True
                        logger.info("transmitted via %s (%d/%d) ch %d",
                                    t.name, i + 1, max(1, t.repeat), ch)
                    else:
                        break  # failed even after a reconnect; stop repeating this radio
        return any_ok

    async def send_manual(self, text: str) -> bool:
        # A composed manual broadcast is a real message for people, so it goes on
        # each radio's LIVE channel (logged as a manual action).
        return await self._send_all(text, manual=True, on_test=False)

    async def send_test(self, text: str) -> bool:
        # The Troubleshoot canned test goes on each radio's TEST channel.
        return await self._send_all(text, manual=True, on_test=True)

    async def load_channels(self, name: str, conn: str, port: str, host: str):
        """Open a transient connection with the given params; read the device's
        channels and model. Returns (channels|None, model_str, error). Frees the
        live port first so it never double-opens, then restores the live link."""
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return None, "", "unknown radio"
            if t.tx is not None:            # release the live connection first
                try:
                    await t.tx.close()
                except Exception:
                    pass
                t.tx, t.connected = None, False
            maker = MeshtasticTransmitter if name == "meshtastic" else MeshCoreTransmitter
            tx = maker(conn or "serial", port or "", host or "")
            try:
                await tx.connect()
                channels = await tx.read_channels()
                model = ""
                try:
                    model = _fmt_model(await tx.read_info())
                except Exception:
                    pass
                result = (channels, model, "")
            except Exception as exc:
                result = (None, "", str(exc))
            finally:
                try:
                    await tx.close()
                except Exception:
                    pass
            # best-effort: bring the live link back so "Connect" doesn't leave it offline
            try:
                await self._ensure(t)
            except Exception:
                pass
            return result

    async def send_to(self, name: str, text: str) -> tuple[bool, str]:
        """Key up a single named radio (bench testing). Returns (ok, error)."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ch = t.test_channel   # tests go on this radio's test channel
            ok, err = await self._try_send(t, text, ch)
            self._db.add_transmit_log(ch, blen, ok, text, True,
                                      error=("" if ok else err), transport=t.name)
            if ok:
                logger.info("test transmitted via %s on ch %d", t.name, ch)
            return ok, ("" if ok else err)

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
