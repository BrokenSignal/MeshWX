"""IPAWS poller: area lockdown must apply to Cancels too.

Regression for the bug where a Cancel resolved its event from its own CAP info
block and was broadcast on the LIVE channel regardless of area -- so a "Local
Area Emergency" cancel for Sauk County, WI went out on a South-Carolina mesh.
Cancels now obey the same area gate as Alert/Update.
"""
import pytest

from app.db import Database
from app.ipaws import IpawsPoller

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"


class FakeTx:
    def __init__(self):
        self.sent = []       # (text, on_test)

    def enqueue_ipaws(self, text, on_test=False, on_result=None):
        self.sent.append((text, on_test))
        return True


def _alert(identifier, msg_type, status, event, area_desc, ugc=None,
           references="", sender="gov@example.gov"):
    ugc_xml = ""
    for code in (ugc or []):
        ugc_xml += ('<geocode><valueName>UGC</valueName>'
                    '<value>%s</value></geocode>' % code)
    return """
  <alert>
    <identifier>{id}</identifier>
    <sender>{sender}</sender>
    <sent>2026-08-05T10:00:00-04:00</sent>
    <status>{status}</status>
    <msgType>{msg}</msgType>
    <references>{refs}</references>
    <info>
      <event>{event}</event>
      <headline>{event}</headline>
      <area>
        <areaDesc>{area}</areaDesc>
        {ugc}
      </area>
    </info>
  </alert>""".format(id=identifier, sender=sender, status=status, msg=msg_type,
                     refs=references, event=event, area=area_desc, ugc=ugc_xml)


def _feed(*alerts):
    body = '<alerts xmlns="%s">%s</alerts>' % (CAP_NS, "".join(alerts))
    return body.encode()


class _FakeResp:
    def __init__(self, content):
        self.status_code = 200
        self.content = content


class _FakeClient:
    payload = b""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeResp(_FakeClient.payload)


@pytest.fixture
def wired(monkeypatch):
    import app.ipaws as ipaws_mod
    monkeypatch.setattr(ipaws_mod.httpx, "AsyncClient", _FakeClient)
    db = Database(":memory:")
    db.set_setting("dry_run", False)          # broadcasting live
    db.set_setting("ipaws_enabled", True)
    db.set_setting("zones", "SCC079,SCC063")  # South Carolina only
    tx = FakeTx()
    return db, IpawsPoller(db, tx), tx


async def test_out_of_area_cancel_is_not_broadcast(wired):
    """A Cancel whose own geocode is out of area must be dropped, even though it
    carries a resolvable event -- the exact Sauk County WI case."""
    _FakeClient.payload = _feed(
        _alert("urn:wi:1", "Cancel", "Actual", "Local Area Emergency",
               "WI Sauk County Emergency Management", ugc=["WIC111"]),
    )
    await wired[1].poll_once()
    db, _, tx = wired
    assert tx.sent == []                                    # nothing on the mesh
    row = db.get_ipaws("urn:wi:1")
    assert row["error"] == "out of area"
    assert not row["transmitted"]


async def test_in_area_alert_then_its_cancel_both_broadcast(wired):
    """An in-area alert broadcasts, and a later Cancel that references it (no
    geocode of its own) still goes out -- lifting a real local alert."""
    _FakeClient.payload = _feed(
        _alert("urn:sc:1", "Alert", "Actual", "Civil Emergency", "Richland",
               ugc=["SCC079"]),
        _alert("urn:sc:1c", "Cancel", "Actual", "Civil Emergency", "Richland",
               references="gov@example.gov,urn:sc:1,2026-08-05T10:00:00-04:00"),
    )
    await wired[1].poll_once()
    db, _, tx = wired
    texts = [t for t, _ in tx.sent]
    assert any("Civil Emergency" in t and "CANCELLED" not in t for t in texts)
    assert any("CANCELLED" in t and "Civil Emergency" in t for t in texts)


async def test_cancel_with_no_geocode_and_unknown_reference_dropped(wired):
    _FakeClient.payload = _feed(
        _alert("urn:zz:1", "Cancel", "Actual", "Some Emergency", "Somewhere",
               references="gov@example.gov,urn:never-seen,2026-08-05T10:00:00-04:00"),
    )
    await wired[1].poll_once()
    db, _, tx = wired
    assert tx.sent == []
    assert db.get_ipaws("urn:zz:1")["error"] == "out of area"


def test_cancel_in_area_helper(wired):
    db, poller, _ = wired
    zones = {"SCC079", "SCC063"}
    # own geocode intersects -> in area
    assert poller._cancel_in_area({"ugc": ["SCC079"], "references": ""}, zones)
    # own geocode elsewhere -> out
    assert not poller._cancel_in_area({"ugc": ["WIC111"], "references": ""}, zones)
    # no geocode, references a stored Alert row -> in area
    db.add_ipaws("urn:sc:9", "gov", "Civil Emergency", "Richland", "", "Alert",
                 "Actual", "2026-08-05T10:00:00-04:00", "text", transmitted=True,
                 error="")
    assert poller._cancel_in_area(
        {"ugc": [], "references": "gov,urn:sc:9,2026-08-05T10:00:00-04:00"}, zones)
    # no geocode, unknown reference -> out
    assert not poller._cancel_in_area(
        {"ugc": [], "references": "gov,urn:nope,2026-08-05T10:00:00-04:00"}, zones)
    # no zones configured -> nothing in area
    assert not poller._cancel_in_area({"ugc": ["SCC079"], "references": ""}, set())
