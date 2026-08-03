"""Regression tests for the resilience/hardening guards on the life-safety path:
the poll watchdog, clock-skew detection, DB durability pragmas, and the
transmit-worker's tolerance of a callback failure."""
import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

import pytest

from app.db import Database
from app.poller import WxPoller, PollerStatus
from app.transmit import TransmitManager


class FakeTx:
    def enqueue(self, text, channel, on_result=None):
        return True


# --------------------------------------------------------------------------
# 1. Poll watchdog: a hung poll_once must be aborted, and the loop must survive.
# --------------------------------------------------------------------------
async def test_watchdog_aborts_hung_poll(monkeypatch):
    import app.poller as poller_mod
    monkeypatch.setattr(poller_mod, "POLL_HARD_TIMEOUT", 0.2)
    db = Database(":memory:")
    db.set_setting("poll_interval", 60)
    poller = WxPoller(db, FakeTx())

    async def hang():
        await asyncio.sleep(999)          # simulate a wedged poll
    poller.poll_once = hang

    task = asyncio.create_task(poller._run())
    await asyncio.sleep(0.6)              # long enough for the watchdog to fire
    still_running = not task.done()       # loop survived the hang
    poller._stopped = True
    poller._wake.set()
    await asyncio.sleep(0.05)
    task.cancel()

    errs = [r["message"] for r in db.recent_errors(20)]
    assert any("hung" in m for m in errs), "watchdog should log the aborted poll"
    assert still_running, "the poll loop must survive a hung poll, not die with it"
    assert "timed out" in poller.status.last_poll_result


# --------------------------------------------------------------------------
# 2. Clock skew: poll_once computes the offset from the NWS server Date header.
# --------------------------------------------------------------------------
async def test_clock_skew_computed(monkeypatch):
    import app.poller as poller_mod

    class FakeNWSDate:
        def __init__(self, *a, **k):
            self.last_server_date = None

        async def fetch_active(self, zones, max_retries=4):
            # Pretend the server clock is 600s BEHIND ours -> positive skew.
            server_now = datetime.now(timezone.utc) - timedelta(seconds=600)
            self.last_server_date = format_datetime(server_now)
            return {"features": []}, "{}"

    monkeypatch.setattr(poller_mod, "NWSClient", FakeNWSDate)
    db = Database(":memory:")
    poller = WxPoller(db, FakeTx())
    await poller.poll_once()

    skew = poller.status.clock_skew_seconds
    assert skew is not None
    assert 570 < skew < 630, f"skew should be ~600s, got {skew}"


# --------------------------------------------------------------------------
# 3. DB durability pragmas are actually applied.
# --------------------------------------------------------------------------
def test_db_pragmas(tmp_path):
    db = Database(str(tmp_path / "mesh.db"))
    assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --------------------------------------------------------------------------
# 4. A failing result callback must not escape and kill the transmit worker.
# --------------------------------------------------------------------------
def test_safe_result_swallows_callback_errors():
    tm = TransmitManager(Database(":memory:"))

    def boom(ok):
        raise RuntimeError("callback blew up")

    # Must not raise -- a broken callback cannot be allowed to crash the worker.
    tm._safe_result(boom, True)


def test_liveness_watchdog_forces_exit_when_stalled(monkeypatch):
    """A stalled event loop (no heartbeat) must trigger a force-exit so the
    restart policy relaunches; a beating loop must NOT."""
    import time
    import app.watchdog as wd

    calls = []
    monkeypatch.setattr(wd.os, "_exit", lambda code: calls.append(code))

    # Stalled: never beat -> should force-exit within a couple check intervals.
    lv = wd.Liveness(stall_seconds=0.3)
    lv.start()
    time.sleep(0.6)
    lv.stop()
    assert calls, "watchdog should force-exit when the loop stalls"

    # Healthy: beat continuously -> must NOT force-exit.
    calls.clear()
    lv2 = wd.Liveness(stall_seconds=0.3)
    lv2.start()
    for _ in range(6):
        lv2.beat()
        time.sleep(0.1)
    lv2.stop()
    assert not calls, "watchdog must not fire while the loop is beating"
