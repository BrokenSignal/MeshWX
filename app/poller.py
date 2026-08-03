"""NWS polling background task.

Owns the poll loop: fetch active alerts, filter, dedupe, format, and hand
transmissions to the TransmitManager. Errors never crash the loop; they are
logged and surfaced to the UI error log. Runtime status is exposed for the
dashboard.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import POLL_INTERVAL_MIN, POLL_HARD_TIMEOUT
from .dedupe import decide
from .filters import FilterRules
from .formatter import build_mesh_text
from .models import Alert
from .nws import NWSClient, NWSError

logger = logging.getLogger("mesh_wx.poller")


class PollerStatus:
    def __init__(self):
        self.last_poll_time: str | None = None
        self.last_poll_success_time: str | None = None   # last poll that actually reached NWS
        self.last_poll_result: str = "not yet polled"
        self.last_raw_response: str = ""
        self.last_broadcast_failure: str | None = None    # last alert that failed on all radios
        self.last_broadcast_failure_text: str = ""
        self.clock_skew_seconds: float | None = None      # system clock vs NWS server time
        self.started_at: datetime = datetime.now(timezone.utc)

    @property
    def uptime_seconds(self) -> int:
        return int((datetime.now(timezone.utc) - self.started_at).total_seconds())


class WxPoller:
    def __init__(self, db, transmit_manager):
        self._db = db
        self._tx = transmit_manager
        self.status = PollerStatus()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="nws-poller")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def poke(self) -> None:
        """Wake the loop early (e.g. after a settings change)."""
        self._wake.set()

    # ---- loop -----------------------------------------------------------
    async def _run(self) -> None:
        while not self._stopped:
            try:
                # Hard watchdog: no single poll may hang the loop. If poll_once
                # wedges (network black-hole, DB lock, a bug), abort it and let the
                # next cycle run -- a stuck poller is a silent blind spot.
                await asyncio.wait_for(self.poll_once(), timeout=POLL_HARD_TIMEOUT)
            except asyncio.TimeoutError:
                self.status.last_poll_result = "error: poll timed out (aborted by watchdog)"
                self._db.add_error("poller", "poll hung and was aborted after %ds" % POLL_HARD_TIMEOUT)
                logger.error("poll_once exceeded %ds; aborted by watchdog", POLL_HARD_TIMEOUT)
            except Exception as exc:  # never let the loop die
                self.status.last_poll_result = f"error: {exc}"
                self._db.add_error("poller", str(exc))
                logger.exception("unexpected poll error")
            interval = max(POLL_INTERVAL_MIN, int(self._db.get_setting("poll_interval", 120)))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def poll_once(self) -> None:
        settings = self._db.all_settings()
        zones = settings.get("zones", "SCZ050")
        contact = settings.get("nws_contact", "")
        client = NWSClient(contact=contact)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            data, raw = await client.fetch_active(zones)
        except NWSError as exc:
            self.status.last_poll_time = now
            self.status.last_poll_result = f"error: {exc}"
            self._db.add_error("nws", str(exc))
            self._db.add_event("ERROR", f"NWS poll failed: {exc}")
            return

        self.status.last_poll_time = now
        self.status.last_poll_success_time = now
        self.status.last_raw_response = raw
        # Clock-skew check: compare our clock to the NWS server's Date header. A
        # skewed VM clock makes "until" times wrong and can break time-based dedup.
        srv = getattr(client, "last_server_date", None)
        if srv:
            try:
                from email.utils import parsedate_to_datetime
                server_dt = parsedate_to_datetime(srv)
                self.status.clock_skew_seconds = (
                    datetime.now(timezone.utc) - server_dt).total_seconds()
            except Exception:
                pass
        features = data.get("features", []) or []
        self.status.last_poll_result = f"ok: {len(features)} active alert(s)"

        self._db.purge_expired_state()
        self._db.prune_history()

        rules = FilterRules.from_settings(settings)
        tz_name = settings.get("display_timezone", "")
        channel = int(settings.get("channel_index", 0))
        dry_run = bool(settings.get("dry_run", True))

        for feature in features:
            try:
                await self._process(feature, rules, tz_name, channel, dry_run)
            except Exception as exc:
                logger.exception("error processing feature")
                self._db.add_error("poller", f"process error: {exc}")

    async def _process(self, feature, rules, tz_name, channel, dry_run) -> None:
        alert = Alert.from_feature(feature)
        if not alert.nws_id:
            return
        decision = decide(alert, rules, self._db.get_state)

        if decision.disposition == "cancelled":
            text = _format_cancel(alert, tz_name)
        else:
            # Build the payload: SPS gets relabelled with its extracted threat;
            # "until" uses the hazard end (alert.ends); upcoming alerts show a
            # start->end window.
            text = build_mesh_text(alert, tz_name)

        # Audit log: record every distinct alert pulled from NOAA exactly once
        # (deduped by nws_id -- we re-poll every cycle), with its disposition and
        # the reason from decide(). Lets any alert be looked up to see whether we
        # received it and why it did or didn't go out. Re-polls add no new row.
        if not self._db.history_exists(alert.nws_id):
            _detail = decision.detail
            _logged_text = ""
            if decision.transmit:
                _logged_text = text
                if dry_run:
                    _detail = f"DRY-RUN: {decision.detail}"
            self._db.add_history(
                alert.nws_id, alert.event, alert.area_desc,
                decision.disposition, _logged_text, _detail,
            )

        if not decision.transmit:
            return

        # Transmit path (history already recorded above).
        if dry_run:
            self._db.add_event("INFO", f"[DRY-RUN] would send: {text}")
            self._record_state(alert, decision)   # unchanged: dedup while paused
        else:
            # Record dedup state (so we don't repeat) ONLY when the broadcast is
            # verified to have gone out. If it fails on every radio, we leave the
            # state UNrecorded so the next poll retries this alert, and raise a
            # loud alarm -- a warning that did not go out must not be forgotten.
            fail_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

            def _on_result(ok, a=alert, d=decision, t=text, ts=fail_ts):
                if ok:
                    self._record_state(a, d)
                else:
                    self.status.last_broadcast_failure = ts
                    self.status.last_broadcast_failure_text = t
                    self._db.add_error(
                        "broadcast", f"NOT SENT on any radio (will retry): {t}")
                    self._db.add_event(
                        "ALARM", f"BROADCAST FAILED, will retry: {a.event} for "
                                 f"{(a.area_desc or '')[:40]}")
                    logger.error("broadcast FAILED on all radios: %s", t)

            self._tx.enqueue(text, channel, on_result=_on_result)
            self._db.add_event("INFO", f"queued {decision.disposition}: {text}")
        logger.info(
            "alert %s -> %s%s", alert.nws_id, decision.disposition,
            " (dry-run)" if dry_run else "",
            extra={"alert_id": alert.nws_id,
                   "disposition": decision.disposition,
                   "action": "dry-run" if dry_run else "queued"},
        )

    def _record_state(self, alert: Alert, decision) -> None:
        # Only called on the transmit path (sent / update / cancelled), so the
        # dedupe row always records a genuine broadcast.
        sent_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._db.upsert_state(
            nws_id=alert.nws_id,
            event=alert.event,
            headline=alert.headline,
            expires=alert.expires,
            msg_hash=alert.content_hash(),
            disposition=decision.disposition,
            sent_ts=sent_ts,
        )


def _format_cancel(alert: Alert, tz_name: str) -> str:
    from .formatter import PREFIX, _area_string
    from .config import MAX_PAYLOAD_BYTES

    area = _area_string(alert.area_desc)
    body = f"CANCELLED: {alert.event}"
    if area:
        body += f": {area}"
    msg = PREFIX + body
    if len(msg.encode()) <= MAX_PAYLOAD_BYTES:
        return msg
    return (PREFIX + f"CANCELLED: {alert.event}")[:MAX_PAYLOAD_BYTES]
