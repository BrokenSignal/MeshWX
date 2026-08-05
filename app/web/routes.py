"""Web UI routes (server-rendered templates + htmx partials)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx

from .. import __version__
from ..config import (MAX_PAYLOAD_BYTES, POLL_INTERVAL_MIN, IPAWS_EVENT_TYPES,
                      GITHUB_LATEST_RELEASE_API, GITHUB_RELEASES_URL)
from ..serial_discovery import list_all_ports


def _template_dir() -> Path:
    """Locate the templates both in a normal run and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):  # packaged (Windows .exe / onedir)
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "web" / "templates"
    return Path(__file__).parent / "templates"


router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(_template_dir()))

from ..formatter import fmt_local
TEMPLATES.env.filters["localtime"] = fmt_local


# History/dashboard status labels describe the FILTER decision, not transmission
# (whether a message actually went out lives in the Transmit Log). So "sent"
# becomes "pass" (the alert passed your rules); the rest already describe the
# alert, not a send.
DISP_LABELS = {
    "sent": "pass",
    "filtered": "filtered",
    "update": "update",
    "cancelled": "cancelled",
    "duplicate": "duplicate",
}


def render(request: Request, name: str, **ctx):
    try:
        tz = request.app.state.db.get_setting("display_timezone", "")
    except Exception:
        tz = ""   # fall back to this machine's local time
    return TEMPLATES.TemplateResponse(
        request, name,
        {"max_bytes": MAX_PAYLOAD_BYTES, "tz": tz, "disp_label": DISP_LABELS,
         "version": __version__, **ctx},
    )


def _db(request: Request):
    return request.app.state.db


def _tx(request: Request):
    return request.app.state.tx


def _poller(request: Request):
    return request.app.state.poller


def _status_flag(request: Request, name: str) -> bool:
    return any(s["connected"] for s in _tx(request).status() if s["name"] == name)


def _status_ctx(request: Request) -> dict:
    db, tx, poller = _db(request), _tx(request), _poller(request)
    return {
        "last_poll_time": poller.status.last_poll_time,
        "last_poll_result": poller.status.last_poll_result,
        "port": tx.port or "(none)",
        "connected": tx.connected,
        "tx_error": tx.last_error,
        "dry_run": bool(db.get_setting("dry_run", True)),
        "queue_depth": tx.queue_depth,
        "uptime": poller.status.uptime_seconds,
        "events": db.recent_events(10),
    }



def _spark(counts):
    if not counts or max(counts) == 0:
        return "", ""
    n, mx, W, H, pad = len(counts), max(counts), 720, 80, 8
    pts = []
    for i, c in enumerate(counts):
        x = 0 if n == 1 else i * (W / (n - 1))
        y = H - pad - (c / mx) * (H - 2 * pad)
        pts.append((x, y))
    line = "M" + " L".join("%.0f,%.0f" % (x, y) for x, y in pts)
    return line, line + " L%d,%d L0,%d Z" % (W, H, H)


def _dash_ctx(request) -> dict:
    db, tx, poller = _db(request), _tx(request), _poller(request)
    tz = db.get_setting("display_timezone", "")
    zones = [z.strip() for z in (db.get_setting("zones", "") or "").split(",") if z.strip()]
    forecast = [z for z in zones if len(z) > 2 and z[2] == "Z"]
    county = [z for z in zones if len(z) > 2 and z[2] == "C"]
    port = tx.port or ""
    device = "Heltec V3" if ("CP210" in port or "Silicon_Labs" in port) else (port.split("/")[-1] if port else "(none)")
    up = poller.status.uptime_seconds
    dd, rem = divmod(up, 86400); hh, rem = divmod(rem, 3600); mm = rem // 60
    uptime_str = ("%dd %dh" % (dd, hh)) if dd else ("%dh %dm" % (hh, mm))
    now = datetime.datetime.now(datetime.timezone.utc)
    def _age(ts):
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds() / 86400.0
        except Exception:
            return 999
    rows = db.query_history(limit=1000)
    sent = [r for r in rows if r["disposition"] in ("sent", "update", "cancelled")]
    sent_7d = sum(1 for r in sent if _age(r["ts"]) < 7)
    sent_today = sum(1 for r in sent if _age(r["ts"]) < 1)
    buckets = [0] * 7
    for r in sent:
        a = _age(r["ts"])
        if 0 <= a < 7:
            buckets[6 - int(a)] += 1
    spark_line, spark_fill = _spark(buckets)
    include = list(db.get_setting("filter_include_exact", []) or [])
    if db.get_setting("filter_include_suffix", []):
        include = ["All Warnings"] + include
    recent = [{
        "event": r["event"], "area": r["area"], "disposition": r["disposition"],
        "detail": r["detail"], "text": r["transmitted_text"], "when": fmt_local(r["ts"], tz),
    } for r in db.query_history(limit=6)]
    ltx = db.query_transmit_log(limit=1)
    last_tx = "-"
    if ltx:
        # ASCII only: this string is auto-escaped through {{ }}, so a non-ASCII
        # separator could mojibake depending on charset. Keep it plain.
        last_tx = ("OK - " if ltx[0]["success"] else "failed - ") + fmt_local(ltx[0]["ts"], tz)

    # ---- health watchdog: catch SILENT failures (looks live, delivers nothing) --
    st = poller.status
    interval = int(db.get_setting("poll_interval", 120))
    dry = bool(db.get_setting("dry_run", True))
    def _age_s(ts):
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return None
    problems = []          # (level, message); level in {"critical","warn"}
    # 1. Are we still reaching NWS? A stale success time = we are blind to alerts.
    succ_age = _age_s(st.last_poll_success_time)
    stale_after = max(interval * 3, 360)
    if succ_age is None:
        problems.append(("warn", "No successful NWS poll yet."))
    elif succ_age > stale_after:
        problems.append(("critical",
            "Not reaching NWS: last good poll %d min ago. Not receiving alerts." % (succ_age // 60)))
    if st.last_poll_result.startswith("error"):
        problems.append(("warn", "Last NWS poll errored: %s" % st.last_poll_result[7:][:80]))
    # 2. Radios: any enabled radio offline means alerts may not go out.
    radios = tx.status()
    on = [r for r in radios if r["enabled"]]
    off = [r for r in on if not r["connected"]]
    if on and len(off) == len(on):
        problems.append(("critical", "All radios offline. Alerts cannot be broadcast."))
    elif off:
        problems.append(("warn", "%s offline." % ", ".join(r["label"] for r in off)))
    if not on:
        problems.append(("critical", "No radios enabled. Nothing will broadcast."))
    # 3. A recent alert that failed on every radio (verified-transmit feedback).
    bf_age = _age_s(st.last_broadcast_failure)
    if bf_age is not None and bf_age < 1800:
        problems.append(("critical",
            "A broadcast FAILED %d min ago and is being retried: %s"
            % (bf_age // 60, (st.last_broadcast_failure_text or "")[:60])))
    # 4. Backed-up queue.
    if tx.queue_depth > 5:
        problems.append(("warn", "Transmit queue backed up (%d waiting)." % tx.queue_depth))
    # 5. System clock skew vs NWS: makes alert "until" times wrong.
    skew = getattr(st, "clock_skew_seconds", None)
    if skew is not None and abs(skew) > 120:
        problems.append(("warn",
            "System clock is off by %d s vs NWS. Alert times may be wrong; check the VM clock."
            % int(abs(skew))))
    health_level = ("critical" if any(l == "critical" for l, _ in problems)
                    else "warn" if problems else "ok")

    return {
        "health_level": health_level,
        "health_problems": [m for _, m in problems],
        "health_paused": dry,
        "dry_run": bool(db.get_setting("dry_run", True)),
        "connected": tx.connected, "device": device, "tx_error": tx.last_error,
        "channel_index": int(db.get_setting("channel_index", 0)),
        "zone_count": len(zones), "forecast_count": len(forecast), "county_count": len(county),
        "poll_interval": int(db.get_setting("poll_interval", 120)), "queue_depth": tx.queue_depth,
        "last_poll_local": fmt_local(poller.status.last_poll_time, tz) if poller.status.last_poll_time else "-",
        "uptime_str": uptime_str, "sent_7d": sent_7d, "sent_today": sent_today,
        "spark_line": spark_line, "spark_fill": spark_fill,
        "include": include, "recent": recent, "last_tx": last_tx,
        "transports": tx.status(),
    }


# ---- liveness probe ----------------------------------------------------
@router.get("/healthz", response_class=PlainTextResponse)
async def healthz(request: Request):
    # If this responds, the event loop is servicing requests (i.e. not wedged).
    # Used by the Docker HEALTHCHECK; deliberately does no DB/radio work so it
    # is a pure liveness signal, not a readiness check.
    return PlainTextResponse("ok")


# ---- dashboard ---------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return render(request, "dashboard.html", **_dash_ctx(request))


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request):
    return render(request, "_dash_top.html", **_dash_ctx(request))


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_cols_partial(request: Request):
    # The recent-alerts list + radios + broadcasting columns, for live polling.
    return render(request, "_dash_cols.html", **_dash_ctx(request))


@router.get("/partials/ipaws", response_class=HTMLResponse)
async def ipaws_partial(request: Request):
    # IPAWS (FEMA) dashboard panel -- separate from the weather pipeline.
    db = _db(request)
    st = getattr(request.app.state, "ipaws", None)
    return render(request, "_ipaws_panel.html", ipaws_rows=db.query_ipaws(30),
                  ipaws_status=(st.status if st else None))


@router.get("/ipaws", response_class=HTMLResponse)
async def ipaws_history(request: Request):
    # Full IPAWS history page (mirrors the NOAA History page).
    db = _db(request)
    st = getattr(request.app.state, "ipaws", None)
    return render(request, "ipaws_history.html", ipaws_rows=db.query_ipaws(200),
                  ipaws_status=(st.status if st else None))


@router.get("/partials/ipaws-history", response_class=HTMLResponse)
async def ipaws_history_partial(request: Request):
    return render(request, "_ipaws_rows.html", ipaws_rows=_db(request).query_ipaws(200))


@router.post("/dry-run/toggle", response_class=HTMLResponse)
async def toggle_dry_run(request: Request):
    db = _db(request)
    db.set_setting("dry_run", not bool(db.get_setting("dry_run", True)))
    db.add_event("INFO", f"dry-run set to {db.get_setting('dry_run')}")
    return render(request, "_dash_top.html", **_dash_ctx(request))


# ---- history -----------------------------------------------------------
@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, disposition: str = "", date_from: str = "",
                  date_to: str = ""):
    rows = _db(request).query_history(
        disposition=disposition or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return render(
        request, "history.html", rows=rows, disposition=disposition,
        date_from=date_from, date_to=date_to,
        dispositions=["sent", "filtered", "duplicate", "update", "cancelled"],
    )


@router.get("/partials/history", response_class=HTMLResponse)
async def history_partial(request: Request, disposition: str = "", date_from: str = "",
                          date_to: str = ""):
    # Just the rows, honoring the same filters, for live polling.
    rows = _db(request).query_history(
        disposition=disposition or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return render(request, "_history_rows.html", rows=rows)


# ---- transmit log ------------------------------------------------------
@router.get("/transmit-log", response_class=HTMLResponse)
async def transmit_log(request: Request):
    rows = _db(request).query_transmit_log()
    return render(request, "transmit_log.html", rows=rows)


@router.get("/partials/transmit-log", response_class=HTMLResponse)
async def transmit_log_partial(request: Request):
    return render(request, "_transmit_rows.html", rows=_db(request).query_transmit_log())


@router.post("/transmit-log/resend/{entry_id}", response_class=HTMLResponse)
async def resend_log(request: Request, entry_id: int):
    db, tx = _db(request), _tx(request)
    row = db.get_transmit_log(entry_id)
    if row is not None and row["transport"]:
        await tx.resend(row["transport"], row["text"] or "", int(row["channel"]))
    return render(request, "_transmit_rows.html", rows=db.query_transmit_log())



_STATES = [
    ("AL","Alabama"),("AK","Alaska"),("AZ","Arizona"),("AR","Arkansas"),("CA","California"),
    ("CO","Colorado"),("CT","Connecticut"),("DE","Delaware"),("DC","District of Columbia"),
    ("FL","Florida"),("GA","Georgia"),("HI","Hawaii"),("ID","Idaho"),("IL","Illinois"),
    ("IN","Indiana"),("IA","Iowa"),("KS","Kansas"),("KY","Kentucky"),("LA","Louisiana"),
    ("ME","Maine"),("MD","Maryland"),("MA","Massachusetts"),("MI","Michigan"),("MN","Minnesota"),
    ("MS","Mississippi"),("MO","Missouri"),("MT","Montana"),("NE","Nebraska"),("NV","Nevada"),
    ("NH","New Hampshire"),("NJ","New Jersey"),("NM","New Mexico"),("NY","New York"),
    ("NC","North Carolina"),("ND","North Dakota"),("OH","Ohio"),("OK","Oklahoma"),("OR","Oregon"),
    ("PA","Pennsylvania"),("RI","Rhode Island"),("SC","South Carolina"),("SD","South Dakota"),
    ("TN","Tennessee"),("TX","Texas"),("UT","Utah"),("VT","Vermont"),("VA","Virginia"),
    ("WA","Washington"),("WV","West Virginia"),("WI","Wisconsin"),("WY","Wyoming"),
    ("PR","Puerto Rico"),("VI","U.S. Virgin Islands"),("GU","Guam"),
]

# Timezones offered in Settings. MeshWX serves the US NWS, so this is the US /
# territories set plus an "Automatic" option (blank = the device's local zone).
_TIMEZONES = [
    ("", "Automatic (this device's time zone)"),
    ("America/New_York", "Eastern (New York)"),
    ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"),
    ("America/Phoenix", "Mountain, no DST (Arizona)"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"),
    ("America/Anchorage", "Alaska"),
    ("America/Adak", "Hawaii-Aleutian (Adak)"),
    ("Pacific/Honolulu", "Hawaii (Honolulu)"),
    ("America/Puerto_Rico", "Atlantic (Puerto Rico, U.S. Virgin Islands)"),
    ("Pacific/Guam", "Chamorro (Guam, Northern Marianas)"),
    ("Pacific/Pago_Pago", "Samoa (American Samoa)"),
    ("UTC", "UTC"),
]

_EVENT_GROUPS = {
    "Warnings": ["Tornado Warning","Severe Thunderstorm Warning","Flash Flood Warning",
        "Flood Warning","Hurricane Warning","Tropical Storm Warning","Storm Surge Warning",
        "Winter Storm Warning","Ice Storm Warning","Blizzard Warning","High Wind Warning",
        "Extreme Heat Warning","Excessive Heat Warning","Red Flag Warning","Dust Storm Warning",
        "Freeze Warning"],
    "Watches": ["Tornado Watch","Severe Thunderstorm Watch","Flash Flood Watch","Flood Watch",
        "Hurricane Watch","Tropical Storm Watch","Winter Storm Watch","High Wind Watch",
        "Fire Weather Watch"],
    "Advisories": ["Special Weather Statement","Severe Weather Statement","Heat Advisory",
        "Wind Advisory","Winter Weather Advisory","Flood Advisory","Dense Fog Advisory",
        "Frost Advisory","Air Quality Alert","Coastal Flood Advisory","Rip Current Statement"],
}


async def _fetch_counties(state: str, contact: str):
    ua = "mesh-wx/1.0 (%s)" % contact if contact else "mesh-wx/1.0"
    url = "https://api.weather.gov/zones?area=%s&type=county" % state
    async with httpx.AsyncClient(timeout=20,
            headers={"User-Agent": ua, "Accept": "application/geo+json"}) as c:
        r = await c.get(url); r.raise_for_status()
        feats = r.json().get("features", [])
    out = [(f["properties"]["id"], f["properties"]["name"]) for f in feats]
    return sorted(out, key=lambda x: x[1])


@router.get("/settings/counties", response_class=HTMLResponse)
async def settings_counties(request: Request, state: str = ""):
    db = _db(request)
    selected = set(z.strip() for z in (db.get_setting("zones", "") or "").split(",") if z.strip())
    counties, err = [], ""
    if state:
        try:
            counties = await _fetch_counties(state, db.get_setting("nws_contact", ""))
        except Exception as exc:
            err = "Could not load counties for %s (%s). Check your internet/NWS contact and retry." % (state, exc)
    return render(request, "_counties.html", counties=counties, selected=selected, state=state, err=err)


# ---- settings ----------------------------------------------------------
def _parse_version(v: str) -> tuple:
    """Leading numeric dotted parts of a version tag as an int tuple, e.g.
    'v1.2.10' -> (1, 2, 10). Stops at the first non-numeric chunk so a
    pre-release suffix ('1.2.0-rc1') compares on its numeric core (1, 2, 0)."""
    core = re.split(r"[-+ ]", (v or "").strip().lstrip("vV"), maxsplit=1)[0]
    parts = []
    for chunk in core.split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    lt, cur = _parse_version(latest), _parse_version(current)
    if not lt:
        return False
    n = max(len(lt), len(cur))
    return lt + (0,) * (n - len(lt)) > cur + (0,) * (n - len(cur))


@router.get("/settings/check-updates", response_class=HTMLResponse)
async def check_updates(request: Request):
    """Compare the running version against the latest GitHub release. Server-side
    fetch (no CORS/CSP issues), read-only, gracefully degrades when offline."""
    ctx = {"current": __version__, "latest": None,
           "url": GITHUB_RELEASES_URL, "state": "error", "detail": ""}
    try:
        async with httpx.AsyncClient(
                timeout=10,
                headers={"User-Agent": "MeshWX-update-check",
                         "Accept": "application/vnd.github+json"}) as client:
            r = await client.get(GITHUB_LATEST_RELEASE_API)
        if r.status_code == 200:
            data = r.json()
            ctx["latest"] = (data.get("tag_name") or "").lstrip("vV")
            ctx["url"] = data.get("html_url") or GITHUB_RELEASES_URL
            ctx["state"] = "update" if _is_newer(ctx["latest"], __version__) else "current"
        elif r.status_code == 404:
            ctx["state"] = "current"       # repo has no published releases yet
            ctx["detail"] = "No releases published yet."
        else:
            ctx["detail"] = "GitHub returned HTTP %d." % r.status_code
    except Exception:
        ctx["detail"] = "Could not reach GitHub. Check this machine's connection."
    return render(request, "_update_check.html", **ctx)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    db = _db(request)
    s = db.all_settings()
    zones = [z.strip() for z in (s.get("zones", "") or "").split(",") if z.strip()]
    counties_sel = [z for z in zones if len(z) > 2 and z[2] == "C"]
    extra = [z for z in zones if not (len(z) > 2 and z[2] == "C")]
    current_state = counties_sel[0][:2] if counties_sel else ""
    county_list = []
    if current_state:
        try:
            county_list = await _fetch_counties(current_state, s.get("nws_contact", ""))
        except Exception:
            county_list = []
    return render(
        request, "settings.html", s=s, min_interval=POLL_INTERVAL_MIN, ports=None,
        states=_STATES, current_state=current_state, state=current_state,
        timezones=_TIMEZONES, tz_current=(s.get("display_timezone", "") or ""),
        tz_known={v for v, _ in _TIMEZONES},
        counties=county_list, selected=set(counties_sel), extra_zones=", ".join(extra),
        event_groups=_EVENT_GROUPS,
        selected_events=set(s.get("filter_include_exact", []) or []),
        all_warnings=bool(s.get("filter_include_suffix", []) or []), err="",
        ipaws_enabled=bool(s.get("ipaws_enabled", True)),
        ipaws_send_tests=bool(s.get("ipaws_send_tests", False)),
        ipaws_event_types=IPAWS_EVENT_TYPES,
        ipaws_selected=set(s.get("ipaws_events", None) if s.get("ipaws_events", None) is not None
                           else [k for k, _l, _kw in IPAWS_EVENT_TYPES]),
        mt_enabled=bool(s.get("meshtastic_enabled", True)),
        mt_conn=s.get("meshtastic_conn", "serial") or "serial",
        mt_host=s.get("meshtastic_host", "") or "",
        mt_channel=int(s.get("channel_index", 0) or 0),
        mt_port=s.get("serial_port", "") or "",
        mc_enabled=bool(s.get("meshcore_enabled", False)),
        mc_conn=s.get("meshcore_conn", "serial") or "serial",
        mc_port=s.get("meshcore_port", "") or "",
        mc_host=s.get("meshcore_host", "") or "",
        mc_channel=int(s.get("meshcore_channel", 0) or 0),
        mt_test=int(s.get("meshtastic_test_channel", 1) or 1),
        mc_test=int(s.get("meshcore_test_channel", 1) or 1),
        mt_channels=s.get("meshtastic_channels", []) or [],
        mc_channels=s.get("meshcore_channels", []) or [],
        mt_connected=_status_flag(request, "meshtastic"),
        mc_connected=_status_flag(request, "meshcore"),
        mt_model=s.get("meshtastic_model", "") or "",
        mc_model=s.get("meshcore_model", "") or "",
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    poll_interval: int = Form(...),
    nws_contact: str = Form(...),
    channel_index: int = Form(0),
    display_timezone: str = Form(""),   # blank = Automatic (device local time)
    serial_port: str = Form(""),
    state: str = Form(""),
    counties: list[str] = Form(default=[]),
    extra_zones: str = Form(""),
    events: list[str] = Form(default=[]),
    all_warnings: str = Form(""),
    ipaws_enabled: str = Form(""),
    ipaws_send_tests: str = Form(""),
    ipaws_events: list[str] = Form(default=[]),
    meshtastic_enabled: str = Form(""),
    meshtastic_conn: str = Form("serial"),
    meshtastic_host: str = Form(""),
    meshcore_enabled: str = Form(""),
    meshcore_conn: str = Form("serial"),
    meshcore_port: str = Form(""),
    meshcore_host: str = Form(""),
    meshcore_channel: int = Form(0),
    meshtastic_test_channel: int = Form(1),
    meshcore_test_channel: int = Form(1),
):
    def _rep(v):
        try:
            return max(1, min(5, int(v)))
        except (TypeError, ValueError):
            return 2
    db, tx, poller = _db(request), _tx(request), _poller(request)
    interval = max(POLL_INTERVAL_MIN, int(poll_interval))

    zone_list = [c.strip() for c in counties if c.strip()]
    zone_list += [z.strip() for z in extra_zones.split(",") if z.strip()]
    db.set_setting("zones", ",".join(zone_list))
    db.set_setting("poll_interval", interval)
    db.set_setting("nws_contact", nws_contact.strip())
    db.set_setting("channel_index", int(channel_index))
    db.set_setting("display_timezone", display_timezone.strip())
    db.set_setting("filter_include_exact", [e for e in events if e])
    db.set_setting("filter_include_suffix", ["Warning"] if all_warnings else [])
    db.set_setting("filter_exclude_exact", [])

    # Radios - Meshtastic + MeshCore, each independently enabled.
    db.set_setting("ipaws_enabled", bool(ipaws_enabled))
    db.set_setting("ipaws_send_tests", bool(ipaws_send_tests))
    db.set_setting("ipaws_events", [e for e in ipaws_events if e])
    db.set_setting("meshtastic_enabled", bool(meshtastic_enabled))
    db.set_setting("meshtastic_conn", (meshtastic_conn or "serial").strip())
    db.set_setting("meshtastic_host", meshtastic_host.strip())
    db.set_setting("serial_port", serial_port.strip())
    db.set_setting("meshcore_enabled", bool(meshcore_enabled))
    db.set_setting("meshcore_conn", (meshcore_conn or "serial").strip())
    db.set_setting("meshcore_port", meshcore_port.strip())
    db.set_setting("meshcore_host", meshcore_host.strip())
    db.set_setting("meshcore_channel", int(meshcore_channel))
    db.set_setting("meshtastic_test_channel", int(meshtastic_test_channel))
    db.set_setting("meshcore_test_channel", int(meshcore_test_channel))

    # Rebuild transports from the new settings and (re)connect the enabled ones.
    await tx.reconfigure()

    poller.poke()
    db.add_event("INFO", "settings saved")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/scan", response_class=HTMLResponse)
async def scan_ports(request: Request, target: str = Form("serial_port")):
    ports = list_all_ports()
    # only allow the two known field ids to be scripted into the page
    field = target if target in ("serial_port", "meshcore_port") else "serial_port"
    return render(request, "_ports.html", ports=ports, target=field)


_RADIO_FIELDS = {
    "meshtastic": ("meshtastic_conn", "serial_port", "meshtastic_host",
                   "channel_index", "meshtastic_test_channel"),
    "meshcore":   ("meshcore_conn", "meshcore_port", "meshcore_host",
                   "meshcore_channel", "meshcore_test_channel"),
}


_INCLUDE_SEL = {
    "meshtastic": "[name='meshtastic_conn'],[name='serial_port'],[name='meshtastic_host']",
    "meshcore": "[name='meshcore_conn'],[name='meshcore_port'],[name='meshcore_host']",
}


def _channel_ctx(db, tx, name, channels=None, model=None, error="", connected=None):
    conn_f, port_f, host_f, live_f, test_f = _RADIO_FIELDS[name]
    if channels is None:  # fall back to the last channels we read from this radio
        channels = db.get_setting(name + "_channels", []) or []
    if model is None:
        model = db.get_setting(name + "_model", "") or ""
    if connected is None:  # reflect the live transport's real state
        connected = any(s["connected"] for s in tx.status() if s["name"] == name)
    return dict(
        radio=name, channels=channels, error=error, connected=connected, model=model,
        include_sel=_INCLUDE_SEL[name],
        live_field=live_f, test_field=test_f,
        live_val=int(db.get_setting(live_f, 0) or 0),
        test_val=int(db.get_setting(test_f, 1) or 1),
    )


@router.post("/settings/channels/{name}", response_class=HTMLResponse)
async def load_channels(request: Request, name: str):
    if name not in _RADIO_FIELDS:
        return PlainTextResponse("unknown radio", status_code=404)
    conn_f, port_f, host_f, _, _ = _RADIO_FIELDS[name]
    form = await request.form()
    conn = form.get(conn_f, "serial")
    port = form.get(port_f, "")
    host = form.get(host_f, "")
    db, tx = _db(request), _tx(request)
    channels, model, error = await tx.load_channels(name, conn, port, host)
    if channels is not None:  # success: cache names + model so they persist across reload/save
        db.set_setting(name + "_channels", channels)
        db.set_setting(name + "_model", model)
        ctx = _channel_ctx(db, tx, name, channels=channels, model=model,
                           error="", connected=True)
    else:  # failed read of THIS device: show the error + saved values, never a stale
        # cached list from a different radio, and don't claim connected.
        ctx = _channel_ctx(db, tx, name, channels=[], model="",
                           error=error or "could not read this radio", connected=False)
    return render(request, "_radio_channels.html", **ctx)


# ---- manual send -------------------------------------------------------
@router.get("/manual", response_class=HTMLResponse)
async def manual_page(request: Request):
    return render(request, "manual_send.html")


@router.post("/manual/send", response_class=HTMLResponse)
async def manual_send(request: Request, text: str = Form(...)):
    tx = _tx(request)
    text = text[:MAX_PAYLOAD_BYTES] if len(text.encode()) > MAX_PAYLOAD_BYTES else text
    # Enforce byte cap defensively (multibyte-safe).
    while len(text.encode()) > MAX_PAYLOAD_BYTES:
        text = text[:-1]
    ok = await tx.send_manual(text)   # goes on each radio's LIVE channel
    msg = "sent" if ok else f"failed: {tx.last_error}"
    return render(request, "_manual_result.html", ok=ok, message=msg,
                  text=text, bytes=len(text.encode()))


# ---- troubleshoot ------------------------------------------------------
@router.get("/troubleshoot", response_class=HTMLResponse)
async def troubleshoot(request: Request):
    return render(request, "troubleshoot.html",
                  errors=_db(request).recent_errors(),
                  transports=_tx(request).status())


@router.post("/troubleshoot/clear-errors", response_class=HTMLResponse)
async def clear_errors(request: Request):
    _db(request).clear_errors()
    return render(request, "_errors.html", errors=[])


@router.post("/troubleshoot/test", response_class=HTMLResponse)
async def send_test(request: Request):
    tx = _tx(request)
    text = "[WX] mesh-wx test message"
    ok = await tx.send_test(text)   # goes on each radio's TEST channel
    return render(
        request, "_manual_result.html", ok=ok,
        message=("test sent to all radios" if ok else f"failed: {tx.last_error}"),
        text=text, bytes=len(text.encode()),
    )


@router.post("/troubleshoot/test/{name}", response_class=HTMLResponse)
async def send_test_one(request: Request, name: str):
    tx = _tx(request)
    label = {t["name"]: t["label"] for t in tx.status()}.get(name, name)
    text = "[WX] mesh-wx test via %s" % label
    ok, err = await tx.send_to(name, text)
    return render(
        request, "_manual_result.html", ok=ok,
        message=(f"test sent via {label}" if ok else f"{label} failed: {err}"),
        text=text, bytes=len(text.encode()),
    )


@router.get("/troubleshoot/raw", response_class=PlainTextResponse)
async def raw_response(request: Request):
    raw = _poller(request).status.last_raw_response
    return raw or "(no NWS response captured yet)"


def _split_lines(value: str) -> list[str]:
    """Parse a textarea/newline- or comma-separated list into clean items."""
    parts: list[str] = []
    for chunk in value.replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts

