"""End-to-end poller: feed captured NWS JSON through poll_once against a real
(in-memory) database and assert what gets recorded across polls.

Policy: EVERY distinct alert pulled from NOAA is logged to history exactly once
(deduped by nws_id -- we re-poll every cycle), recording its disposition and the
reason, whether or not it was broadcast. Re-polls of the same id add no new row."""
import pytest

from app.db import Database
from app.poller import WxPoller


class FakeTx:
    def __init__(self):
        self.sent = []

    def enqueue(self, text, channel):
        self.sent.append((text, channel))
        return True


def _future(feat: dict) -> dict:
    props = feat["properties"]
    for key in ("effective", "expires", "ends", "onset"):
        if props.get(key):
            props[key] = props[key].replace("2024-", "2099-")
    return feat


def _collection(features):
    return {"features": [_future(f) for f in features]}


class FakeNWS:
    queue = []

    def __init__(self, *a, **k):
        pass

    async def fetch_active(self, zones, max_retries=4):
        data = FakeNWS.queue.pop(0)
        return data, "{}"


def _dispositions(db, nws_suffix):
    rows = db.query_history(limit=500)
    return [r["disposition"] for r in rows if nws_suffix in (r["nws_id"] or "")]


@pytest.fixture
def wired(monkeypatch, feature):
    import app.poller as poller_mod

    monkeypatch.setattr(poller_mod, "NWSClient", FakeNWS)
    db = Database(":memory:")
    poller = WxPoller(db, FakeTx())
    return db, poller, feature


async def test_logs_all_alerts_once_dedupe_update_cancel(wired):
    db, poller, feature = wired
    ffw = feature("flash_flood_warning")

    # Poll 1: mix of includable + droppable alerts (dry-run ON by default).
    FakeNWS.queue = [_collection([
        feature("tornado_warning"),
        feature("tornado_watch"),
        feature("severe_tstorm_watch"),
        feature("lake_wind_advisory"),
        ffw,
        feature("special_weather_statement"),
    ])]
    await poller.poll_once()

    rows = db.query_history(limit=500)
    disp = [r["disposition"] for r in rows]
    assert disp.count("sent") == 3        # 2 tornado + flash flood warning
    assert disp.count("filtered") == 3    # tstorm watch, lake wind, SWS -- LOGGED with reason
    # every filtered row records WHY it didn't go out
    assert all(r["detail"] for r in rows if r["disposition"] == "filtered")

    # Poll 2: identical feed -> deduped by id. No new rows, no "duplicate" rows.
    n_before = len(db.query_history(limit=500))
    FakeNWS.queue = [_collection([feature("tornado_warning"), ffw])]
    await poller.poll_once()
    assert len(db.query_history(limit=500)) == n_before          # nothing re-logged
    assert _dispositions(db, "ffw400") == ["sent"]               # still one row
    assert "duplicate" not in [r["disposition"] for r in db.query_history(limit=500)]

    # Poll 3: official Update (new id, changed content) -> logged once.
    FakeNWS.queue = [_collection([feature("flash_flood_warning_update")])]
    await poller.poll_once()
    assert _dispositions(db, "ffw401") == ["update"]

    # Poll 4: cancellation (new id) -> logged once.
    FakeNWS.queue = [_collection([feature("flash_flood_warning_cancel")])]
    await poller.poll_once()
    assert _dispositions(db, "ffw402") == ["cancelled"]

    assert poller._tx.sent == []  # dry-run: nothing actually transmitted


async def test_active_alert_logged_once_across_polls(wired):
    """An alert that stays active across many polls is logged exactly once
    (deduped by id), not once per poll -- here a filtered SWS under default rules."""
    db, poller, feature = wired
    sws = feature("special_weather_statement")
    FakeNWS.queue = [_collection([sws]), _collection([sws]), _collection([sws])]
    await poller.poll_once()
    await poller.poll_once()
    await poller.poll_once()
    filtered = [r for r in db.query_history(limit=500) if r["disposition"] == "filtered"]
    assert len(filtered) == 1
    assert filtered[0]["detail"]  # reason recorded
