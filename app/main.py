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
from .transmit import TransmitManager
from .web.routes import router

logger = logging.getLogger("mesh_wx.main")


async def _startup_serial(db: Database, tx: TransmitManager) -> None:
    """Auto-discover a Meshtastic serial node if enabled and none saved, then
    connect every enabled transport (Meshtastic + MeshCore)."""
    mt_serial = (bool(db.get_setting("meshtastic_enabled", True))
                 and (db.get_setting("meshtastic_conn", "serial") or "serial") == "serial")
    if mt_serial and not db.get_setting("serial_port", ""):
        logger.info("no serial port saved; scanning for a node")
        from .serial_discovery import discover_port
        loop = asyncio.get_event_loop()
        found = await loop.run_in_executor(None, discover_port)
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

    app.state.cfg = cfg
    app.state.db = db
    app.state.tx = tx
    app.state.poller = poller

    tx.start()
    await _startup_serial(db, tx)
    poller.start()

    try:
        yield
    finally:
        logger.info("shutting down mesh-wx")
        await poller.stop()
        await tx.stop()
        db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="mesh-wx", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_bootstrap()
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_config=None)


if __name__ == "__main__":
    main()
