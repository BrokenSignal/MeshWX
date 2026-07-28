"""Format an alert into a Meshtastic text payload (<= 195 bytes).

    "[WX] Tornado Warning for Charleston and surrounding areas until 8:45 PM"
    "[WX] Heat Advisory for Columbia and surrounding areas from 12:00 PM to 8:00 PM"
    "[WX] SPS: Strong thunderstorm (60 mph wind, 0.75in hail) - Columbia until 8:00 PM"

Multi-county alerts are summarised as "<home area> and surrounding areas",
anchored on the configured home area when it is one of the alert's counties
(otherwise the first listed county). Times are local (tz abbrev dropped -- the
mesh is regional). Upcoming alerts (onset in the future) show a start->end
window; in-effect alerts show only "until <end>". A generic Special Weather
Statement is relabelled "SPS: <threat>" from its NWSheadline so the mesh says
what it's actually for. The payload is byte-capped in UTF-8, area trimmed first.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MAX_PAYLOAD_BYTES

PREFIX = "[WX] "
DEFAULT_HOME_AREA = "Columbia"


def _to_local(iso: str, tz_name: str):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    try:
        dt = dt.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        pass
    return dt


def _clock(dt) -> str:
    try:
        return dt.strftime("%-I:%M %p").strip()
    except ValueError:
        return dt.strftime("%I:%M %p").lstrip("0").strip()


def _now_local(tz_name: str):
    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _format_when(onset_iso: str, ends_iso: str, tz_name: str) -> str:
    start = _to_local(onset_iso, tz_name)
    end = _to_local(ends_iso, tz_name)
    now = _now_local(tz_name)
    upcoming = False
    if start is not None and now is not None:
        try:
            upcoming = start > now
        except TypeError:
            upcoming = False
    if upcoming:
        return f"from {_clock(start)} to {_clock(end)}" if end is not None else f"from {_clock(start)}"
    if end is not None:
        return f"until {_clock(end)}"
    return ""


def _area_string(area_desc: str, home_area: str = DEFAULT_HOME_AREA) -> str:
    areas = [a.strip() for a in area_desc.split(";") if a.strip()]
    if not areas:
        return ""
    if len(areas) == 1:
        return areas[0]
    primary = next(
        (a for a in areas if home_area and home_area.lower() in a.lower()),
        areas[0],
    )
    return f"{primary} and surrounding areas"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def format_alert(
    event: str,
    area_desc: str,
    ends_iso: str,
    tz_name: str = "America/New_York",
    onset_iso: str = "",
    home_area: str = DEFAULT_HOME_AREA,
    sep: str = "for",
    max_bytes: int = MAX_PAYLOAD_BYTES,
) -> str:
    when = _format_when(onset_iso, ends_iso, tz_name)
    area = _area_string(area_desc, home_area)

    def assemble(area_part: str) -> str:
        body = event
        if area_part:
            body += f" {sep} {area_part}"
        if when:
            body += f" {when}"
        return PREFIX + body

    msg = assemble(area)
    if _byte_len(msg) <= max_bytes:
        return msg
    primary_only = area.replace(" and surrounding areas", "")
    msg = assemble(primary_only)
    if _byte_len(msg) <= max_bytes:
        return msg
    msg = assemble("")
    if _byte_len(msg) <= max_bytes:
        return msg
    return _truncate_bytes(msg, max_bytes)


def build_mesh_text(alert, tz_name: str = "America/New_York",
                    max_bytes: int = MAX_PAYLOAD_BYTES) -> str:
    """Payload for a non-cancel alert. A Special Weather Statement with an
    extracted threat is relabelled 'SPS: <threat>' (separator '-'); everything
    else uses '<event> for <area>'."""
    event = alert.event
    detail = getattr(alert, "detail", "") or ""
    if event == "Special Weather Statement" and detail:
        return format_alert(f"SPS: {detail}", alert.area_desc, alert.ends, tz_name,
                            onset_iso=alert.onset, sep="-", max_bytes=max_bytes)
    return format_alert(event, alert.area_desc, alert.ends, tz_name,
                        onset_iso=alert.onset, sep="for", max_bytes=max_bytes)


def fmt_local(iso: str, tz_name: str = "America/New_York") -> str:
    """Human-friendly local timestamp for the UI, e.g. "Jul 28, 1:40 AM".
    Falls back to the raw value if it cannot be parsed."""
    dt = _to_local(iso, tz_name)
    if dt is None:
        return iso or ""
    try:
        return dt.strftime("%b %-d, %-I:%M %p")
    except ValueError:
        return dt.strftime("%b %d, %I:%M %p")
