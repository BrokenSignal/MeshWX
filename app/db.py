"""SQLite storage: settings, alert state (dedupe), history, transmit log, errors.

The database is the source of truth. A single connection is shared across the
asyncio loop and the transmit worker thread, guarded by a lock. SQLite calls
are fast local operations, so running them synchronously is fine.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_SETTINGS, STATE_EXPIRY_HOURS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
    nws_id      TEXT PRIMARY KEY,
    event       TEXT,
    headline    TEXT,
    expires     TEXT,
    msg_hash    TEXT,
    sent_ts     TEXT,
    disposition TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    nws_id           TEXT,
    event            TEXT,
    area             TEXT,
    disposition      TEXT,
    transmitted_text TEXT,
    detail           TEXT
);

CREATE TABLE IF NOT EXISTS transmit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    channel    INTEGER,
    byte_count INTEGER,
    success    INTEGER,
    manual     INTEGER,
    text       TEXT,
    error      TEXT,
    transport  TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    source TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT,
    message TEXT
);

-- IPAWS (FEMA) alerts: kept fully separate from the NWS weather pipeline above.
CREATE TABLE IF NOT EXISTS ipaws_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    identifier  TEXT UNIQUE,
    sender      TEXT,
    event       TEXT,
    area        TEXT,
    headline    TEXT,
    msg_type    TEXT,
    status      TEXT,
    sent        TEXT,
    text        TEXT,
    transmitted INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);
CREATE INDEX IF NOT EXISTS idx_history_disp ON history(disposition);
CREATE INDEX IF NOT EXISTS idx_txlog_ts ON transmit_log(ts);
CREATE INDEX IF NOT EXISTS idx_ipaws_ts ON ipaws_log(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # A write that hits a lock should WAIT (up to 5s) for it to clear rather
        # than fail instantly with "database is locked".
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self._init_schema()
        self._seed_settings()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _seed_settings(self) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT key FROM settings")
            existing = {r["key"] for r in cur.fetchall()}
            for key, value in DEFAULT_SETTINGS.items():
                if key not in existing:
                    self._conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?)",
                        (key, json.dumps(value)),
                    )
            self._conn.commit()

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def all_settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    # ---- alert dedupe state --------------------------------------------
    def get_state(self, nws_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM alert_state WHERE nws_id = ?", (nws_id,)
            ).fetchone()

    def upsert_state(
        self,
        nws_id: str,
        event: str,
        headline: str,
        expires: str,
        msg_hash: str,
        disposition: str,
        sent_ts: Optional[str],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO alert_state
                   (nws_id, event, headline, expires, msg_hash, sent_ts,
                    disposition, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(nws_id) DO UPDATE SET
                       event=excluded.event,
                       headline=excluded.headline,
                       expires=excluded.expires,
                       msg_hash=excluded.msg_hash,
                       sent_ts=COALESCE(excluded.sent_ts, alert_state.sent_ts),
                       disposition=excluded.disposition,
                       updated_at=excluded.updated_at""",
                (nws_id, event, headline, expires, msg_hash, sent_ts,
                 disposition, _now()),
            )
            self._conn.commit()

    def purge_expired_state(self) -> int:
        """Remove state rows 48h past their alert expiry."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_EXPIRY_HOURS)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alert_state WHERE expires IS NOT NULL "
                "AND expires != '' AND expires < ?",
                (cutoff.isoformat(),),
            )
            self._conn.commit()
            return cur.rowcount

    # ---- history --------------------------------------------------------
    def add_history(
        self,
        nws_id: str,
        event: str,
        area: str,
        disposition: str,
        transmitted_text: str = "",
        detail: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history(ts, nws_id, event, area, disposition, "
                "transmitted_text, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), nws_id, event, area, disposition,
                 transmitted_text, detail),
            )
            self._conn.commit()

    def history_exists(self, nws_id: str, disposition: Optional[str] = None) -> bool:
        # True if a history row exists for this alert id (optionally a specific
        # disposition). With disposition=None it dedupes by id alone, so each
        # alert pulled from NOAA is logged only once despite re-polling.
        with self._lock:
            if disposition is None:
                row = self._conn.execute(
                    "SELECT 1 FROM history WHERE nws_id = ? LIMIT 1",
                    (nws_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM history WHERE nws_id = ? AND disposition = ? "
                    "LIMIT 1",
                    (nws_id, disposition),
                ).fetchone()
        return row is not None

    def prune_history(self, keep_days: int = 90) -> int:
        # Delete history rows older than keep_days; bounds long-term growth.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM history WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    def query_history(
        self,
        disposition: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if disposition:
            clauses.append("disposition = ?")
            params.append(disposition)
        if date_from:
            clauses.append("ts >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("ts <= ?")
            params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()

    # ---- transmit log ---------------------------------------------------
    def add_transmit_log(
        self,
        channel: int,
        byte_count: int,
        success: bool,
        text: str,
        manual: bool = False,
        error: str = "",
        transport: str = "meshtastic",
    ) -> None:
        cols = ("ts, channel, byte_count, success, manual, text, error, transport")
        vals = (_now(), channel, byte_count, int(success), int(manual), text, error, transport)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO transmit_log(%s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)" % cols, vals)
            except sqlite3.OperationalError:
                # migrate an older DB that predates the transport column
                self._conn.execute("ALTER TABLE transmit_log ADD COLUMN transport TEXT")
                self._conn.execute(
                    "INSERT INTO transmit_log(%s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)" % cols, vals)
            self._conn.commit()

    def query_transmit_log(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transmit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_transmit_log(self, entry_id: int):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transmit_log WHERE id = ?", (entry_id,)
            ).fetchone()

    # ---- errors ---------------------------------------------------------
    def add_error(self, source: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO errors(ts, source, message) VALUES (?, ?, ?)",
                (_now(), source, message),
            )
            self._conn.commit()

    def recent_errors(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def clear_errors(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM errors")
            self._conn.commit()
            return cur.rowcount

    # ---- events (dashboard feed) ---------------------------------------
    def add_event(self, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, level, message) VALUES (?, ?, ?)",
                (_now(), level, message),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # ---- IPAWS (separate from the weather pipeline) --------------------
    def ipaws_seen(self, identifier: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM ipaws_log WHERE identifier = ?", (identifier,)
            ).fetchone() is not None

    def add_ipaws(self, identifier, sender, event, area, headline, msg_type,
                  status, sent, text, transmitted, error="") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ipaws_log(ts, identifier, sender, event, area, "
                "headline, msg_type, status, sent, text, transmitted, error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), identifier, sender, event, area, headline, msg_type,
                 status, sent, text, 1 if transmitted else 0, error),
            )
            self._conn.commit()

    def update_ipaws(self, identifier: str, transmitted: bool, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ipaws_log SET transmitted = ?, error = ? WHERE identifier = ?",
                (1 if transmitted else 0, error, identifier))
            self._conn.commit()

    def get_ipaws(self, identifier: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM ipaws_log WHERE identifier = ?", (identifier,)
            ).fetchone()

    def query_ipaws(self, limit: int = 60) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM ipaws_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def prune_ipaws(self, keep_days: int = 14) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ipaws_log WHERE ts < datetime('now', ?)",
                ("-%d days" % keep_days,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
