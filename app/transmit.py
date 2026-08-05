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
import time
from collections import deque
from dataclasses import dataclass

from .config import BURST_GAP_SECONDS, REPEAT_GAP_SECONDS, QUEUE_MAX, MAX_PAYLOAD_BYTES


class TxUnsent(Exception):
    """Raised when a radio did not actually transmit a message. `category` tells
    the sender WHY, so it can apply the right correction instead of blindly
    retrying:
      'duty_cycle' - radio hit its airtime/duty-cycle cap: wait, then retry
      'queue_full' - the radio's TX queue is full: wait for it to drain, retry
      'too_large'  - message exceeds the radio payload: shrink it (unrecoverable here)
      'no_channel' - the configured channel index does not exist: config error
      'link'       - radio interface down / no response: reconnect, then retry
      'unsent'     - radio accepted the command but did not key up: wait/reconnect
    """
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category
        self.detail = detail

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
        self._qstatus = {}   # firmware TX-queue verdict per packet id: {id: res}

    async def connect(self) -> None:
        if self._iface is not None:
            await self.close()

        def _open():
            if self.conn == "tcp":
                from meshtastic.tcp_interface import TCPInterface
                h, _, p = (self.host or "").partition(":")
                if p:  # explicit host:port
                    iface = TCPInterface(hostname=h, portNumber=int(p))
                else:
                    iface = TCPInterface(hostname=h)
            else:
                from meshtastic.serial_interface import SerialInterface
                iface = SerialInterface(devPath=self.port)
            # Hook the firmware's per-packet TX-queue status so each send can be
            # verified: the radio replies with a QueueStatus for the packet id,
            # res==0 means it was accepted into the TX queue (will transmit),
            # res!=0 means it was rejected/dropped (the silent-drop failure).
            self._qstatus = {}
            orig = iface._handleQueueStatusFromRadio
            def _hook(qs):
                try:
                    if len(self._qstatus) > 256:
                        self._qstatus.clear()
                    self._qstatus[qs.mesh_packet_id] = (qs.res, qs.free)
                except Exception:
                    pass
                return orig(qs)
            iface._handleQueueStatusFromRadio = _hook
            return iface

        self._iface = await asyncio.get_event_loop().run_in_executor(None, _open)

    async def send_text(self, text: str, channel: int) -> None:
        if self._iface is None:
            raise RuntimeError("not connected")

        # res -> (category, name). The radio tells us WHY a packet was not queued.
        cat_by_res = {4: "link", 6: "no_channel", 7: "too_large", 8: "link",
                      9: "duty_cycle"}

        def _send_and_verify():
            pkt = self._iface.sendText(text, channelIndex=channel)
            pid = getattr(pkt, "id", None)
            if pid is None:
                return  # no id to track: fall back to best-effort
            waited = 0.0
            while waited < 6.0:               # await the firmware's verdict
                if pid in self._qstatus:
                    res, free = self._qstatus.pop(pid)
                    if res == 0:
                        return                # accepted into TX queue -> transmits
                    from meshtastic import mesh_pb2
                    try:
                        name = mesh_pb2.Routing.Error.Name(res)
                    except Exception:
                        name = "res=%s" % res
                    cat = cat_by_res.get(res, "unsent")
                    if free == 0 and cat == "unsent":
                        cat = "queue_full"
                    raise TxUnsent(cat, "radio did not transmit (%s)" % name)
                time.sleep(0.15)
                waited += 0.15
            raise TxUnsent("link", "radio did not transmit (no queue confirmation)")

        await asyncio.get_event_loop().run_in_executor(None, _send_and_verify)

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
        # The device's OK/timeout is NOT proof of RF -- a channel broadcast has no
        # ACK, so an "OK" only means the command was accepted, not that the radio
        # keyed up. Confirm the actual transmission by watching the radio's own
        # flood-TX counter advance (proven: it ticks by 1 per real send, and stays
        # flat when the radio does not transmit).
        from meshcore import EventType
        before = await self._flood_tx()
        res = await self._mc.commands.send_chan_msg(channel, text)
        # Capture the device's own error reason, if it gave one, for diagnostics.
        reason = ""
        if getattr(res, "type", None) == EventType.ERROR:
            payload = getattr(res, "payload", {}) or {}
            reason = payload.get("reason", "") if isinstance(payload, dict) else ""
        if before is None:
            return  # counter unreadable: fall back to best-effort (never block sends)
        for _ in range(15):                       # poll up to ~4.5s
            await asyncio.sleep(0.3)
            after = await self._flood_tx()
            if after is not None and after > before:
                return                            # verified on the air (flood_tx advanced)
        detail = "radio did not transmit (TX counter did not advance%s)" % (
            "; %s" % reason if reason else "")
        raise TxUnsent("unsent", detail)

    async def _flood_tx(self):
        try:
            res = await self._mc.commands.get_stats_packets()
            p = getattr(res, "payload", {}) or {}
            v = p.get("flood_tx")
            return int(v) if v is not None else None
        except Exception:
            return None

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
    on_test: bool = False      # False = live channel (weather), True = test channel (IPAWS)
    log_tx: bool = True        # write the weather transmit_log (False for IPAWS -- logged separately)
    on_result: object = None   # optional callable(ok: bool, err: str) invoked after the send


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
        self._queue: deque[QueueItem] = deque(maxlen=QUEUE_MAX)        # high: weather/live
        self._queue_low: deque[QueueItem] = deque(maxlen=QUEUE_MAX)    # low: IPAWS/test
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
        err = await self._open_once(t)
        if not err:
            self._reconnect_delay = 2.0
            self._db.add_event("INFO", "%s connected (%s)" % (t.label, t.target))
            logger.info("%s connected at %s", t.name, t.target)
            return True
        t.error = err
        self._db.add_error(t.name, err)
        logger.warning("%s connect failed: %s", t.name, err)
        return False

    async def _open_once(self, t: Transport) -> str:
        """Open t's link once. Returns '' on success or an error string. No DB
        logging so callers (startup vs. reconnect) can decide how to report."""
        try:
            tx = t.make()
            await tx.connect()
            t.tx, t.connected, t.error = tx, True, ""
            return ""
        except Exception as exc:
            t.tx, t.connected = None, False
            return "connect failed: %s" % exc

    @staticmethod
    def _is_port_locked(err: str) -> bool:
        e = err.lower()
        return ("lock" in e or "busy" in e or "errno 11" in e
                or "errno 16" in e or "resource temporarily unavailable" in e)

    # ---- sending --------------------------------------------------------
    def enqueue(self, text: str, channel: int | None = None, on_result=None) -> bool:
        """Queue a weather alert (HIGH priority, live channel). on_result(ok, err)
        fires after the send with the REAL verified outcome."""
        return self._enqueue(self._queue, text, on_test=False, log_tx=True,
                             on_result=on_result)

    def enqueue_ipaws(self, text: str, on_test: bool = False, on_result=None) -> bool:
        """Queue an IPAWS alert (LOW priority: weather always sends first). Real
        alerts go on the live channel (on_test=False); test/exercise messages go
        on the test channel (on_test=True). Not written to the weather transmit_log."""
        return self._enqueue(self._queue_low, text, on_test=on_test, log_tx=False,
                             on_result=on_result)

    def _enqueue(self, lane, text, on_test, log_tx, on_result) -> bool:
        dropped = len(lane) == lane.maxlen
        if dropped:
            # The item we are about to drop never gets a send: report it failed so
            # the caller does not record it as delivered.
            oldest = lane[0] if lane else None
            if oldest is not None and oldest.on_result is not None:
                self._safe_result(oldest.on_result, False, "dropped (queue full)")
        lane.append(QueueItem(text=text, on_test=on_test, log_tx=log_tx, on_result=on_result))
        self._queue_event.set()
        if dropped:
            logger.warning("transmit queue full; dropped oldest")
            self._db.add_event("WARN", "transmit queue full; dropped oldest message")
        return not dropped

    def _safe_result(self, cb, ok: bool, err: str = "") -> None:
        try:
            r = cb(ok, err)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
        except Exception:
            logger.exception("on_result callback error")

    async def _reconnect(self, t: Transport) -> bool:
        """Reset a transport's link, tolerating a zombie / locked serial port.
        A just-closed port often has not released its fd yet, so an instant reopen
        fails with '[Errno 11] could not exclusively lock port', and close() itself
        can hang on a wedged reader thread. So: close firmly with a timeout, then
        reopen with backoff, waiting out a still-locked port instead of giving up."""
        t.connected = False
        if t.tx is not None:
            try:
                await asyncio.wait_for(t.tx.close(), timeout=8)  # don't hang on a stuck close
            except Exception:
                pass
        t.tx = None
        last = "not reopened"
        for i, delay in enumerate((0.5, 1.5, 3.0, 5.0)):
            await asyncio.sleep(delay)          # give the OS time to release the port
            err = await self._open_once(t)
            if not err:
                self._db.add_event(
                    "INFO", "%s link reset%s (%s)" % (
                        t.label, (" after %d tries" % (i + 1)) if i else "", t.target))
                logger.info("%s reconnected at %s", t.name, t.target)
                return True
            last = err
            if self._is_port_locked(err):
                logger.warning("%s port still locked (try %d/4), waiting: %s",
                               t.name, i + 1, err)
            else:
                logger.warning("%s reopen failed (try %d/4): %s", t.name, i + 1, err)
        t.error = last
        self._db.add_error(t.name, "link reset failed: %s" % last)
        return False

    async def _try_send(self, t: Transport, text: str, ch: int) -> tuple[bool, str]:
        """Send and confirm the radio actually transmitted. On failure, read WHY
        (TxUnsent.category) and apply the matching correction before retrying:
        wait out an airtime/queue limit, reconnect a dead link, or stop early on a
        content/config error that a retry cannot fix. Returns (ok, error)."""
        last = "not connected"
        for attempt in (1, 2, 3):
            if t.tx is None or not t.connected:
                if not await self._ensure(t):
                    last = t.error or "not connected"
                    await asyncio.sleep(1)
                    continue
            try:
                await t.tx.send_text(text, ch)
                if attempt > 1:
                    self._db.add_event(
                        "INFO", "%s sent on attempt %d (%s)" % (t.label, attempt, last))
                return True, ""
            except TxUnsent as exc:
                last = exc.detail
                t.error = last
                cat = exc.category
                logger.warning("%s not sent (attempt %d/3): %s [%s]",
                               t.name, attempt, exc.detail, cat)
                if cat in ("too_large", "no_channel"):
                    break  # a retry cannot fix bad content/config; fail fast with the reason
                if cat == "duty_cycle":
                    await asyncio.sleep(6)          # airtime cap: let the radio cool down
                elif cat == "queue_full":
                    await asyncio.sleep(3)          # let the TX queue drain
                elif cat == "link":
                    await self._reconnect(t)        # interface down / no reply: reopen it
                else:                               # "unsent": brief wait, then reconnect
                    await asyncio.sleep(2)
                    if attempt >= 2:
                        await self._reconnect(t)
            except Exception as exc:
                # Unexpected link error (Broken pipe, serial hiccup): reconnect + retry.
                last = "send failed: %s" % exc
                t.error = last
                logger.warning("%s send error (attempt %d/3): %s", t.name, attempt, exc)
                await self._reconnect(t)
        self._db.add_error(t.name, last)   # a real failure only after all corrections tried
        return False, last

    async def _send_all(self, text: str, manual: bool, on_test: bool | None = None) -> bool:
        any_ok = False
        # Validate before keying any radio: trim to the payload cap (multibyte-safe)
        # so a too-long message never gets silently rejected by the firmware.
        while len(text.encode()) > MAX_PAYLOAD_BYTES:
            text = text[:-1]
        blen = len(text.encode())
        # `on_test` picks the channel (test vs live); `manual` only tags the log
        # (auto vs manual). Automated alerts AND composed manual sends both go on
        # each radio's LIVE channel; only the Troubleshoot test uses the test channel.
        use_test = manual if on_test is None else on_test
        async with self._lock:
            # Each enabled radio gets ONE send that is VERIFIED to have gone out
            # (the send path retries internally on failure/no-transmit). No blind
            # repeats: a message is logged "sent" only when the radio confirmed it
            # actually keyed up, otherwise "failed" so the miss is visible.
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.test_channel if use_test else t.channel
                ok, err = await self._try_send(t, text, ch)
                self._db.add_transmit_log(ch, blen, ok, text, manual,
                                          error=("" if ok else err), transport=t.name)
                if ok:
                    any_ok = True
                    logger.info("transmitted via %s on ch %d (verified)", t.name, ch)
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

    async def resend(self, name: str, text: str, channel: int) -> tuple[bool, str]:
        """Re-transmit an exact message on a specific radio and channel, logging a
        fresh entry. Backs the transmit-log Resend button."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ok, err = await self._try_send(t, text, channel)
            self._db.add_transmit_log(channel, blen, ok, text, True,
                                      error=("" if ok else err), transport=t.name)
            if ok:
                logger.info("resent via %s on ch %d", t.name, channel)
            return ok, ("" if ok else err)

    async def _transmit_item(self, item: QueueItem) -> tuple[bool, str]:
        """Send one queued item on the right channel (live vs test), verified per
        radio. Weather items also write the transmit_log; IPAWS items do not."""
        blen = len(item.text.encode())
        any_ok, last = False, ""
        async with self._lock:
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.test_channel if item.on_test else t.channel
                ok, err = await self._try_send(t, item.text, ch)
                if item.log_tx:
                    self._db.add_transmit_log(ch, blen, ok, item.text, False,
                                              error=("" if ok else err), transport=t.name)
                any_ok = any_ok or ok
                if not ok:
                    last = err
        return any_ok, ("" if any_ok else last)

    async def _worker(self) -> None:
        first = True
        while not self._stopped:
            if not self._queue and not self._queue_low:
                self._queue_event.clear()
                try:
                    await self._queue_event.wait()
                except asyncio.CancelledError:
                    return
                first = True
                continue
            if not first:
                try:
                    await asyncio.sleep(BURST_GAP_SECONDS)   # pace EVERY send, both lanes
                except asyncio.CancelledError:
                    return
            # Weather (high) always drains before IPAWS (low), so a real warning
            # never waits behind a backlog of secondary alerts.
            item = self._queue.popleft() if self._queue else self._queue_low.popleft()
            try:
                ok, err = await self._transmit_item(item)
                if item.on_result is not None:
                    self._safe_result(item.on_result, ok, err)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failure here (e.g. a DB write erroring on a full disk) must not
                # kill the worker -- that would silently stop ALL future broadcasts.
                logger.exception("transmit worker iteration error")
                ok = False
            first = False
            if not ok:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)
