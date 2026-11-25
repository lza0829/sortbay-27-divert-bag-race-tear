from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app import config
from app.config import DB_PATH

_SYNCHRONOUS = os.getenv("SORTBAY_SYNCHRONOUS", "NORMAL").upper()
if _SYNCHRONOUS not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
    _SYNCHRONOUS = "NORMAL"
_JOURNAL_MODE = os.getenv("SORTBAY_JOURNAL_MODE", "WAL").upper()
if _JOURNAL_MODE not in {"WAL", "DELETE", "MEMORY", "TRUNCATE"}:
    _JOURNAL_MODE = "WAL"

_INITIAL_DB_PATH = DB_PATH


def active_db_path() -> Path:
    """Resolve the DB path while tolerating common test patch targets.

    Tests variously monkeypatch ``app.db.DB_PATH`` (the historical by-value
    import) or ``app.config.DB_PATH``.  An explicit patch on this module wins;
    otherwise read config dynamically so a config-only patch also takes effect.
    """
    patched_here = globals().get("DB_PATH")
    if patched_here is not None and patched_here != _INITIAL_DB_PATH:
        return Path(patched_here)
    return Path(config.DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  name TEXT NOT NULL,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  UNIQUE(business_day, name)
);
CREATE TABLE IF NOT EXISTS chutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '口门开',
  soft_cap INTEGER NOT NULL DEFAULT 200,
  UNIQUE(line_id, code),
  FOREIGN KEY(line_id) REFERENCES lines(id)
);
CREATE TABLE IF NOT EXISTS route_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_code TEXT NOT NULL UNIQUE,
  chute_id INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT NOT NULL,
  dest_code TEXT NOT NULL DEFAULT '',
  shift_id INTEGER NOT NULL,
  line_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待路由',
  chute_id INTEGER,
  bag_id INTEGER,
  divert_batch_id INTEGER,
  version INTEGER NOT NULL DEFAULT 1,
  scanned_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(line_id) REFERENCES lines(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS route_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '回流中',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '集包中',
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  scan_id INTEGER NOT NULL,
  from_status TEXT NOT NULL,
  from_bag_id INTEGER,
  bag_receipt_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(batch_id, scan_id),
  FOREIGN KEY(batch_id) REFERENCES divert_batches(id),
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(from_bag_id) REFERENCES bags(id)
);
CREATE TABLE IF NOT EXISTS operation_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_no TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  scan_id INTEGER NOT NULL,
  bag_id INTEGER,
  batch_id INTEGER,
  status TEXT NOT NULL DEFAULT '有效',
  summary TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  reversed_by_receipt_id INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(bag_id) REFERENCES bags(id),
  FOREIGN KEY(batch_id) REFERENCES divert_batches(id),
  FOREIGN KEY(reversed_by_receipt_id) REFERENCES operation_receipts(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scans_barcode_shift
  ON scans(barcode, shift_id);
CREATE INDEX IF NOT EXISTS ix_scans_bag ON scans(bag_id);
CREATE INDEX IF NOT EXISTS ix_scans_status_shift ON scans(shift_id, status);
CREATE INDEX IF NOT EXISTS ix_receipts_scan ON operation_receipts(scan_id, kind, status);
CREATE INDEX IF NOT EXISTS ix_divert_items_scan ON divert_items(scan_id);
"""


def connect() -> sqlite3.Connection:
    db_path = active_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        timeout=30.0,
        isolation_level=None,  # autocommit; db() owns explicit BEGIN IMMEDIATE
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(f"PRAGMA synchronous={_SYNCHRONOUS}")
    return conn


@contextmanager
def db(write: bool = True):
    """Open one unit of work.

    Write units use BEGIN IMMEDIATE so lock acquisition is the single ordering
    point for the 回流/入袋 race.  All receipts, state rows and audit entries
    for one outcome commit or roll back together.
    """
    conn = connect()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        else:
            # One snapshot for a whole dashboard/reconciliation/replay read.
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    additions = {
        "scans": [("version", "INTEGER NOT NULL DEFAULT 1")],
        "audit_events": [
            ("shift_id", "INTEGER"),
            ("detail_json", "TEXT NOT NULL DEFAULT '{}'"),
        ],
    }
    for table, columns in additions.items():
        existing = _table_columns(conn, table)
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # Backfill receipts/items for databases created before the closure ledger.
    conn.execute(
        """
        INSERT OR IGNORE INTO operation_receipts(
          receipt_no, kind, scan_id, bag_id, status, summary, created_at
        )
        SELECT 'RCPT-BACKFILL-BAG-' || s.id, '入袋', s.id, s.bag_id, '有效',
               '升级前已在袋状态补登记', COALESCE(s.scanned_at, datetime('now'))
        FROM scans s
        WHERE s.bag_id IS NOT NULL
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO divert_items(
          batch_id, scan_id, from_status, from_bag_id, created_at
        )
        SELECT s.divert_batch_id, s.id, '已路由', NULL,
               COALESCE(s.scanned_at, datetime('now'))
        FROM scans s
        WHERE s.divert_batch_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM divert_items di
            WHERE di.batch_id=s.divert_batch_id AND di.scan_id=s.id
          )
        """
    )


def init_db() -> None:
    db_path = active_db_path()
    if str(db_path) != ":memory:":
        # Journal mode is persistent; set it once, outside a transaction.
        admin = connect()
        try:
            admin.execute(f"PRAGMA journal_mode={_JOURNAL_MODE}")
        finally:
            admin.close()
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
