from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from app.config import DB_PATH

# WAL 下多读单写并发；写事务靠 busy_timeout 排队，配合 BEGIN IMMEDIATE 串行化
BUSY_TIMEOUT_MS = 5000
MAX_LOCK_RETRIES = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def db():
    """读上下文：连接级惰性事务，异常回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class _ImmediateTx:
    """
    写上下文：autocommit + 显式 BEGIN IMMEDIATE，立即拿写锁，把所有开充/关账/
    急停多写串行化在单事务内；任一步抛错整笔回滚，回到操作前快照。

    锁重试只发生在 __enter__ 取锁阶段（此时尚未向调用方交出连接、无副作用，
    可安全重开连接重试）；一旦取得写锁、进入 with 体后再出错，只回滚不重试，
    避免业务副作用被重复执行。
    """

    def __enter__(self):
        attempt = 0
        while True:
            self.conn = connect()
            self.conn.isolation_level = None  # autocommit，事务边界完全显式
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            except sqlite3.OperationalError as e:
                self.conn.close()
                if "locked" in str(e).lower() and attempt < MAX_LOCK_RETRIES:
                    attempt += 1
                    time.sleep(0.02 * attempt)
                    continue
                raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


def tx() -> _ImmediateTx:
    return _ImmediateTx()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
