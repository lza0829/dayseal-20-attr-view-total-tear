from __future__ import annotations
import sqlite3

from app import attribution
from app.db import connect


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tills (
  id INTEGER PRIMARY KEY,
  till_code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_lines (
  id INTEGER PRIMARY KEY,
  line_no TEXT NOT NULL UNIQUE,
  till_code TEXT NOT NULL,
  business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  amount REAL NOT NULL,
  pay_method TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_lines_day ON cash_lines(business_day);
CREATE TABLE IF NOT EXISTS attribution_corrections (
  id INTEGER PRIMARY KEY,
  line_id INTEGER NOT NULL,
  line_no TEXT NOT NULL,
  action TEXT NOT NULL,
  declared_business_day TEXT NOT NULL,
  occurred_day TEXT NOT NULL,
  stored_business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attribution_corrections_day
  ON attribution_corrections(stored_business_day);
CREATE INDEX IF NOT EXISTS idx_attribution_corrections_line
  ON attribution_corrections(line_id);
CREATE TABLE IF NOT EXISTS close_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  business_day TEXT NOT NULL,
  system_total REAL NOT NULL,
  declared_cash REAL NOT NULL,
  variance REAL NOT NULL,
  line_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  sealed_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  variance REAL NOT NULL,
  disposition TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variance_holds (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
'''

# 归属策略的第二道防线：库里的 business_day 只能是 occurred_at 的发生日。
# 服务层已按 attribution.resolve() 裁决；触发器保证绕过服务层直接写库也写不出脏分区。
GUARDS = '''
CREATE TRIGGER IF NOT EXISTS trg_cash_lines_attribution_insert
BEFORE INSERT ON cash_lines
WHEN NEW.business_day <> substr(NEW.occurred_at, 1, 10)
BEGIN
  SELECT RAISE(ABORT, '流水营业日必须以 occurred_at 的发生日为准');
END;

CREATE TRIGGER IF NOT EXISTS trg_cash_lines_attribution_update
BEFORE UPDATE OF business_day, occurred_at ON cash_lines
WHEN NEW.business_day <> substr(NEW.occurred_at, 1, 10)
BEGIN
  SELECT RAISE(ABORT, '流水营业日必须以 occurred_at 的发生日为准');
END;
'''


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _sealed_days(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT business_day FROM close_batches WHERE status='sealed'"
    ).fetchall()
    return {row["business_day"] for row in rows}


def _migrate(conn: sqlite3.Connection) -> int:
    """把旧库收拢到当前归属口径；可重复执行，返回本次校正的流水数。

    旧数据里 ``business_day`` 与 ``occurred_at`` 不一致的行，按生效策略归入
    发生日，并补写归属校正留痕。已封账营业日（无论旧日还是新日）一律不改：
    改它会与已封批次的合计撕裂，留给巡检接口报告。
    """
    conn.executescript(GUARDS)
    # 旧库可能已经写入了脏行；先放开触发器，改完再重建，保证迁移能跑。
    conn.executescript(
        '''DROP TRIGGER IF EXISTS trg_cash_lines_attribution_insert;
           DROP TRIGGER IF EXISTS trg_cash_lines_attribution_update;'''
    )
    sealed = _sealed_days(conn)
    rows = conn.execute(
        "SELECT id, line_no, business_day, occurred_at FROM cash_lines "
        "WHERE business_day <> substr(occurred_at, 1, 10)"
    ).fetchall()
    repaired = 0
    now = attribution_now()
    for row in rows:
        occurred = attribution.try_occurrence_day(row["occurred_at"])
        if occurred is None or occurred == row["business_day"]:
            continue
        if row["business_day"] in sealed or occurred in sealed:
            # 已封分区不可再挪动；巡检接口会报告为待人工处理。
            continue
        conn.execute(
            "UPDATE cash_lines SET business_day=? WHERE id=?", (occurred, row["id"])
        )
        conn.execute(
            '''INSERT INTO attribution_corrections(
                 line_id, line_no, action, declared_business_day, occurred_day,
                 stored_business_day, occurred_at, reason, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                row["id"],
                row["line_no"],
                attribution.ACTION_REPAIR,
                row["business_day"],
                occurred,
                occurred,
                row["occurred_at"],
                "启动迁移：历史流水营业日与 occurred_at 不一致，归入发生日",
                now,
            ),
        )
        repaired += 1
    conn.executescript(GUARDS)
    return repaired


def attribution_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) c FROM tills").fetchone()["c"]
        if n:
            conn.commit()
            return
        conn.execute("INSERT INTO settings(key, value) VALUES ('business_day', ?)", ("2026-11-16",))
        tills = [
            ("TILL-A", "一号收银"),
            ("TILL-B", "二号收银"),
            ("TILL-C", "三号收银"),
        ]
        for code, title in tills:
            conn.execute("INSERT INTO tills(till_code, title) VALUES (?,?)", (code, title))
        lines = [
            ("CL-0001", "TILL-A", "2026-11-16", "2026-11-16T09:12:00Z", 35.5, "现金", "早市"),
            ("CL-0002", "TILL-A", "2026-11-16", "2026-11-16T10:05:00Z", 128.0, "微信", None),
            ("CL-0003", "TILL-B", "2026-11-16", "2026-11-16T11:20:00Z", 66.0, "现金", None),
            ("CL-0004", "TILL-B", "2026-11-16", "2026-11-16T14:02:00Z", 210.0, "支付宝", None),
            ("CL-0005", "TILL-C", "2026-11-16", "2026-11-16T16:40:00Z", 49.9, "现金", None),
            ("CL-0006", "TILL-A", "2026-11-15", "2026-11-15T18:10:00Z", 88.0, "现金", "昨日"),
        ]
        for row in lines:
            conn.execute(
                '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[3]),
            )
        conn.commit()
    finally:
        conn.close()
