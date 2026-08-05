"""FastAPI application: lifespan wiring + web UI routes."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import load_bootstrap
from .db import Database
from .logging_setup import setup_logging
from .poller import WxPoller
from .ipaws import IpawsPoller
from .transmit import TransmitManager
from .watchdog import Liveness
from .web.routes import router

logger = logging.getLogger("mesh_wx.main")


async def _heartbeat(liveness: Liveness) -> None:
    """Refresh the liveness heartbeat while the event loop is healthy."""
    while True:
        liveness.beat()
        await asyncio.sleep(5)


async def _startup_serial(db: Database, tx: TransmitManager) -> None:
    """Auto-discover a Meshtastic serial node if enabled and none saved, then
    connect every enabled transport (Meshtastic + MeshCore)."""
    mt_serial = (bool(db.get_setting("meshtastic_enabled", True))
                 and (db.get_setting("meshtastic_conn", "serial") or "serial") == "serial")
    if mt_serial and not db.get_setting("serial_port", ""):
        logger.info("no serial port saved; scanning for a node")
        from .serial_discovery import discover_port
        # Never probe a port the user assigned to MeshCore (probing locks it).
        exclude = {db.get_setting("meshcore_port", "") or ""}
        loop = asyncio.get_event_loop()
        found = await loop.run_in_executor(None, lambda: discover_port(exclude))
        if found:
            db.set_setting("serial_port", found)
            db.add_event("INFO", f"auto-discovered node at {found}")
        else:
            db.add_event("WARN", "no node found during startup scan")
            logger.warning("no node found during startup scan")
    await tx.reconfigure()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_bootstrap()
    setup_logging()
    logger.info("starting mesh-wx (db=%s)", cfg.db_path)

    db = Database(cfg.db_path)
    tx = TransmitManager(db)
    poller = WxPoller(db, tx)
    ipaws = IpawsPoller(db, tx)

    app.state.cfg = cfg
    app.state.db = db
    app.state.tx = tx
    app.state.poller = poller
    app.state.ipaws = ipaws

    # Liveness watchdog: force a restart if the event loop ever wedges.
    liveness = Liveness(stall_seconds=90.0)
    liveness.start()
    beat_task = asyncio.create_task(_heartbeat(liveness))
    app.state.liveness = liveness

    tx.start()
    # Connect the radios in the background so serial probing never blocks the
    # web server from coming up (a non-node USB port can take 30s+ to time out).
    startup_task = asyncio.create_task(_startup_serial(db, tx))
    app.state.startup_task = startup_task
    poller.start()
    ipaws.start()

    try:
        yield
    finally:
        logger.info("shutting down mesh-wx")
        liveness.stop()          # first: never force-exit during a clean shutdown
        beat_task.cancel()
        if not startup_task.done():
            startup_task.cancel()
        await poller.stop()
        await ipaws.stop()
        await tx.stop()
        db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="mesh-wx", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import sys
    import uvicorn

    # On Windows, asyncio defaults to the ProactorEventLoop, but the MeshCore
    # serial layer (serial_asyncio) only works on the SelectorEventLoop. Without
    # this, MeshCore serial never connects on Windows ("no response").
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    cfg = load_bootstrap()
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_config=None)


if __name__ == "__main__":
    main()
