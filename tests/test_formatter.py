"""Message formatting, the upcoming/active window, SPS threat labelling, and the 195-byte cap."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import MAX_PAYLOAD_BYTES
from app.formatter import format_alert, build_mesh_text
from app.models import Alert


def test_basic_format(feature):
    a = Alert.from_feature(feature("tornado_warning"))
    msg = format_alert(a.event, a.area_desc, a.ends, "America/New_York", onset_iso=a.onset)
    assert msg.startswith("[WX] Tornado Warning for Charleston")
    assert "and surrounding areas" in msg
    assert "until 8:45 PM" in msg
    assert "EDT" not in msg


def test_single_area(feature):
    a = Alert.from_feature(feature("lake_wind_advisory"))
    msg = format_alert(a.event, a.area_desc, a.ends, "America/New_York", onset_iso=a.onset)
    assert "and surrounding areas" not in msg
    assert msg == "[WX] Lake Wind Advisory for Lake Murray until 8:00 PM"


def test_timezone_conversion(feature):
    a = Alert.from_feature(feature("tornado_warning"))
    msg = format_alert(a.event, a.area_desc, a.ends, "America/Chicago", onset_iso=a.onset)
    assert "7:45 PM" in msg
    assert "CDT" not in msg and "EDT" not in msg


def test_upcoming_alert_shows_window():
    tz = "America/New_York"
    now = datetime.now(ZoneInfo(tz))
    start = (now + timedelta(hours=3)).isoformat()
    end = (now + timedelta(hours=8)).isoformat()
    msg = format_alert("Heat Advisory", "Columbia", end, tz, onset_iso=start)
    assert msg.startswith("[WX] Heat Advisory for Columbia from ")
    assert " to " in msg
    assert "until" not in msg


def test_active_alert_shows_until_only():
    tz = "America/New_York"
    now = datetime.now(ZoneInfo(tz))
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=2)).isoformat()
    msg = format_alert("Severe Thunderstorm Warning", "Richland", end, tz, onset_iso=start)
    assert "until" in msg
    assert "from " not in msg


def _sps_feature(nwsheadline=None, wind=None, hail=None, area="Lexington"):
    params = {}
    if nwsheadline is not None:
        params["NWSheadline"] = [nwsheadline]
    if wind is not None:
        params["maxWindGust"] = [wind]
    if hail is not None:
        params["maxHailSize"] = [hail]
    return {"id": "urn:oid:test.sps", "properties": {
        "event": "Special Weather Statement", "areaDesc": area,
        "effective": "2024-05-20T14:00:00-04:00", "expires": "2024-05-20T18:30:00-04:00",
        "ends": None, "messageType": "Alert", "parameters": params}}


def test_sps_threat_and_impacts():
    a = Alert.from_feature(_sps_feature(
        nwsheadline="A STRONG THUNDERSTORM WILL AFFECT LEXINGTON...RICHLAND COUNTIES",
        wind="60 MPH", hail="0.75"))
    assert a.detail == "Strong thunderstorm (60 mph wind, 0.75in hail)"
    msg = build_mesh_text(a, "America/New_York")
    assert msg == "[WX] SPS: Strong thunderstorm (60 mph wind, 0.75in hail) - Lexington until 6:30 PM"


def test_sps_threat_no_impacts():
    a = Alert.from_feature(_sps_feature(
        nwsheadline="A LINE OF THUNDERSTORMS WITH TORRENTIAL DOWNPOURS WILL AFFECT BOYD"))
    assert a.detail == "Line of thunderstorms with torrential downpours"
    msg = build_mesh_text(a, "America/New_York")
    assert msg.startswith("[WX] SPS: Line of thunderstorms with torrential downpours - Lexington")
    assert "Special Weather Statement" not in msg


def test_sps_no_headline_falls_back():
    a = Alert.from_feature(_sps_feature())  # no NWSheadline, no impacts
    assert a.detail == ""
    msg = build_mesh_text(a, "America/New_York")
    assert msg == "[WX] Special Weather Statement for Lexington until 6:30 PM"


def test_byte_cap_enforced():
    areas = "; ".join(f"County Number {i}" for i in range(200))
    msg = format_alert("Flash Flood Warning", areas,
                       "2024-05-20T22:00:00-04:00", "America/New_York")
    assert len(msg.encode("utf-8")) <= MAX_PAYLOAD_BYTES


def test_byte_cap_with_multibyte_area():
    areas = "; ".join("Ñoño Municipio café" for _ in range(50))
    msg = format_alert("Severe Weather Warning", areas,
                       "2024-05-20T22:00:00-04:00", "America/New_York")
    assert len(msg.encode("utf-8")) <= MAX_PAYLOAD_BYTES
    msg.encode("utf-8").decode("utf-8")


def test_no_end_omits_time():
    msg = format_alert("Tornado Warning", "Charleston", "", "America/New_York")
    assert "until" not in msg and "from" not in msg
    assert msg == "[WX] Tornado Warning for Charleston"


def test_fmt_local():
    from app.formatter import fmt_local
    assert fmt_local("2026-07-28T05:40:28+00:00", "America/New_York") == "Jul 28, 1:40 AM"
    assert fmt_local("", "America/New_York") == ""
