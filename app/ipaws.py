"""IPAWS (FEMA) alert pipeline -- EXPERIMENTAL, VM-only for now.

Deliberately separate from the NWS weather poller/pipeline. Polls FEMA's public
IPAWS-OPEN feed (CAP 1.2 XML over HTTP), drops weather alerts (those already go
out via the NWS pipeline), and re-broadcasts the rest on each radio's TEST
channel so they can be evaluated without touching live weather alerting.

Scope for now (per owner): non-weather, all areas (no geo filter yet), test
channel only. Area lockdown by state comes later. FEMA asks for polling no more
often than every 2 minutes -- enforced as a hard floor.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

import re

from .config import (IPAWS_BASE_URL, IPAWS_PATH, IPAWS_POLL_SECONDS,
                     IPAWS_POLL_FLOOR, IPAWS_LOOKBACK_SECONDS,
                     IPAWS_WEATHER_SENDER_HINTS, IPAWS_DEMO_PATTERNS,
                     IPAWS_EVENT_TYPES, MAX_PAYLOAD_BYTES)


def _category(event: str) -> str:
    """Map an IPAWS event name to one of the IPAWS_EVENT_TYPES keys."""
    low = (event or "").lower()
    for key, _label, kws in IPAWS_EVENT_TYPES:
        if kws and any(k in low for k in kws):
            return key
    return "other"

_DEMO_CODE = re.compile(r"^\s*\d{4,}_")   # e.g. "200120_External_(LIVE_DATA)_..."

logger = logging.getLogger("mesh_wx.ipaws")

CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"


class IpawsStatus:
    def __init__(self):
        self.last_poll_time: str | None = None
        self.last_result: str = "not yet polled"
        self.received: int = 0        # non-weather alerts acted on (this run)
        self.transmitted: int = 0


class IpawsPoller:
    def __init__(self, db, transmit_manager):
        self._db = db
        self._tx = transmit_manager
        self.status = IpawsStatus()
        self._task: asyncio.Task | None = None
        self._stopped = False
        # first poll looks back a short window; then we advance by last poll time
        self._since = datetime.now(timezone.utc) - timedelta(seconds=IPAWS_LOOKBACK_SECONDS)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ipaws-poller")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _interval(self) -> int:
        return max(IPAWS_POLL_FLOOR, IPAWS_POLL_SECONDS)   # never faster than 2 min

    async def _run(self) -> None:
        while not self._stopped:
            if self._db.get_setting("ipaws_enabled", True):
                try:
                    await asyncio.wait_for(self.poll_once(), timeout=90)
                except asyncio.TimeoutError:
                    self.status.last_result = "error: poll timed out"
                    logger.warning("ipaws poll timed out")
                except Exception as exc:
                    self.status.last_result = f"error: {exc}"
                    self._db.add_error("ipaws", str(exc))
                    logger.exception("ipaws poll error")
            try:
                await asyncio.sleep(self._interval())
            except asyncio.CancelledError:
                return

    async def poll_once(self) -> None:
        now = datetime.now(timezone.utc)
        since = self._since
        url = "%s/%s/recent/%s" % (
            IPAWS_BASE_URL, IPAWS_PATH, since.strftime("%Y-%m-%dT%H:%M:%SZ"))
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers={"Accept": "application/xml"})
        if resp.status_code != 200:
            self.status.last_result = f"error: HTTP {resp.status_code}"
            self._db.add_error("ipaws", f"HTTP {resp.status_code}")
            return
        alerts = self._parse(resp.content)
        # advance the window (small overlap so a boundary alert is not missed;
        # dedupe on identifier handles the overlap).
        self._since = now - timedelta(seconds=30)
        self.status.last_poll_time = now.isoformat(timespec="seconds")

        dry_run = bool(self._db.get_setting("dry_run", True))
        send_tests = bool(self._db.get_setting("ipaws_send_tests", False))
        # Which categories to broadcast. None (never configured) = all of them.
        selected = self._db.get_setting("ipaws_events", None)
        # The user's configured counties/zones (shared with the weather side),
        # used to lock IPAWS down to their area.
        zones = set(z.strip().upper() for z in
                    (self._db.get_setting("zones", "") or "").split(",") if z.strip())
        acted = 0
        for a in alerts:
            if self._db.ipaws_seen(a["identifier"]):
                continue
            if a["is_weather"]:
                continue                      # weather already goes out via NWS
            if _is_demo(a):
                continue                      # DMOPEN proficiency demos are not real
            if a["status"] == "Actual":
                pass
            elif a["status"] in ("Test", "Exercise") and send_tests:
                pass                          # opt-in: broadcast tests on the test channel
            else:
                continue                      # System, or tests when the toggle is off
            if a["msg_type"] == "Cancel":
                # Cancels matter (e.g. lifting a shelter-in-place). Resolve what is
                # being cancelled; log-only if unidentifiable.
                text = self._cancel_text(a)
                event = a["event"] or "Cancel"
                if text is None:
                    self._db.add_ipaws(a["identifier"], a["sender"], a["event"], a["area"],
                                       a["headline"], a["msg_type"], a["status"], a["sent"],
                                       "", transmitted=False, error="cancel (unresolved)")
                    continue
            elif a["msg_type"] in ("Alert", "Update"):
                # Only broadcast selected alert categories (cancels are exempt --
                # they resolve to a prior alert we chose to broadcast, or drop out).
                if selected is not None and _category(a["event"]) not in selected:
                    continue
                # Area lockdown: only alerts covering the user's configured
                # counties/zones. No zones set = broadcast nothing on the live
                # channel. This also drops junk with no valid geocode.
                if not zones or not (set(a["ugc"]) & zones):
                    continue
                text = _mesh_text(a)
                event = a["event"]
            else:
                continue                      # Ack/Error etc.

            # Broadcasting paused (dry-run): log what WOULD go out, but do not
            # transmit -- IPAWS honors the same pause as weather.
            if dry_run:
                self._db.add_ipaws(a["identifier"], a["sender"], event, a["area"],
                                   a["headline"], a["msg_type"], a["status"], a["sent"],
                                   text, transmitted=False, error="broadcasting paused")
                acted += 1
                continue
            # Record the row as queued, then hand off to the SHARED transmit queue
            # (low priority: weather always sends first). Real alerts go on the
            # live channel; test/exercise stay on the test channel.
            on_test = a["status"] in ("Test", "Exercise")
            self._db.add_ipaws(a["identifier"], a["sender"], event, a["area"],
                               a["headline"], a["msg_type"], a["status"], a["sent"],
                               text, transmitted=False, error="queued")
            self._tx.enqueue_ipaws(text, on_test=on_test,
                                   on_result=self._make_result_cb(a["identifier"]))
            acted += 1
        self.status.received += acted
        self.status.last_result = "ok: %d alert(s), %d new non-weather" % (len(alerts), acted)
        self._db.prune_ipaws()

    def _parse(self, raw: bytes) -> list:
        out = []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.warning("ipaws xml parse error: %s", exc)
            return out
        for a in root.findall(".//" + CAP + "alert"):
            g = lambda t: (a.findtext(CAP + t) or "").strip()
            sender = g("sender")
            references = g("references")
            info = a.find(CAP + "info")
            event = area = headline = description = instruction = ""
            ugc = []
            if info is not None:
                event = (info.findtext(CAP + "event") or "").strip()
                headline = (info.findtext(CAP + "headline") or "").strip()
                description = (info.findtext(CAP + "description") or "").strip()
                instruction = (info.findtext(CAP + "instruction") or "").strip()
                descs = []
                for ar in info.findall(CAP + "area"):
                    d = (ar.findtext(CAP + "areaDesc") or "").strip()
                    if d:
                        descs.append(d)
                    for gc in ar.findall(CAP + "geocode"):
                        vn = (gc.findtext(CAP + "valueName") or "").strip().upper()
                        val = (gc.findtext(CAP + "value") or "").strip().upper()
                        if vn == "UGC" and val:
                            ugc.append(val)
                area = "; ".join(descs)
            low = sender.lower()
            is_weather = any(h in low for h in IPAWS_WEATHER_SENDER_HINTS)
            out.append({
                "identifier": g("identifier"), "sender": sender, "sent": g("sent"),
                "status": g("status"), "msg_type": g("msgType"),
                "event": event, "area": area, "headline": headline,
                "description": description, "instruction": instruction,
                "ugc": ugc, "references": references, "is_weather": is_weather,
            })
        return out

    def _make_result_cb(self, identifier: str):
        """Callback the transmit worker invokes with the verified outcome."""
        def cb(ok: bool, err: str = ""):
            self._db.update_ipaws(identifier, ok, "" if ok else (err or "send failed"))
            if ok:
                self.status.transmitted += 1
        return cb

    def _cancel_text(self, a: dict):
        """Build a mesh message for a Cancel. Cancels often have no info block, so
        resolve WHAT is being cancelled from the referenced original alert (which
        we likely broadcast). Returns None if we cannot identify it."""
        event, area = a["event"], a["area"]
        if not event:
            for ref in _ref_identifiers(a["references"]):
                row = self._db.get_ipaws(ref)
                if row is not None:
                    event = row["event"] or event
                    area = area or (row["area"] or "")
                    break
        if not event:
            return None                      # unknown cancellation -> not useful to broadcast
        body = "[IPAWS] CANCELLED: %s%s" % (event, (" for " + area) if area else "")
        while len(body.encode()) > MAX_PAYLOAD_BYTES:
            body = body[:-1]
        return body


def _ref_identifiers(references: str) -> list:
    """CAP references are 'sender,identifier,sent' triples, space-separated.
    Pull out the identifier from each."""
    out = []
    for ref in (references or "").split():
        parts = ref.split(",")
        if len(parts) >= 2:
            out.append(parts[1].strip())
    return out


def _is_demo(a: dict) -> bool:
    """True for IPAWS proficiency/demonstration messages (status=Actual but not
    real emergencies) -- matched by their event/identifier naming conventions."""
    hay = ("%s %s" % (a.get("event", ""), a.get("identifier", ""))).lower()
    if any(p in hay for p in IPAWS_DEMO_PATTERNS):
        return True
    return bool(_DEMO_CODE.match(a.get("event", "") or ""))


def _ascii(s: str) -> str:
    """Collapse whitespace and drop non-ASCII so the mesh message is clean."""
    s = " ".join((s or "").split())
    return "".join(c for c in s if 32 <= ord(c) < 127).strip()


def _mesh_text(a: dict) -> str:
    """Short, ASCII, <= payload cap. Leads with the event + area (the quick scan),
    then the actual CONTEXT (headline/description) so people know WHAT the
    emergency is, not just its category. Tagged [IPAWS]."""
    event = _ascii(a.get("event") or "") or "Alert"
    area = _ascii(a.get("area") or "")
    tag = "IPAWS TEST" if a.get("status") in ("Test", "Exercise") else "IPAWS"
    # The substance of the alert. Prefer the human headline; fall back to the
    # description. Drop it if it just restates the event category.
    detail = _ascii(a.get("headline") or "") or _ascii(a.get("description") or "")
    if detail and detail.lower().rstrip(".") == event.lower().rstrip("."):
        detail = _ascii(a.get("description") or "")
        if detail.lower().rstrip(".") == event.lower().rstrip("."):
            detail = ""

    body = "[%s] %s%s" % (tag, event, (" for " + area) if area else "")
    if detail:
        body = "%s: %s" % (body, detail)
    b = body.encode()
    if len(b) > MAX_PAYLOAD_BYTES:
        body = b[:MAX_PAYLOAD_BYTES - 3].decode("ascii", "ignore").rstrip() + "..."
    return body
