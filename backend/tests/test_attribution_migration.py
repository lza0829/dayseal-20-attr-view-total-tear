"""旧库迁移：历史脏行按生效策略归入发生日，已封分区不动。"""
from __future__ import annotations

import sqlite3

import pytest


LEGACY_SCHEMA = '''
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tills (id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL);
CREATE TABLE cash_lines (
  id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
  business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
  pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE close_batches (
  id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
  system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
  line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
);
CREATE TABLE variance_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
  variance REAL NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL
);
'''


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    import app.config as config
    import app.db as db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "dayseal.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO settings(key,value) VALUES('business_day','2026-11-16')")
        conn.execute("INSERT INTO tills(till_code,title) VALUES('TILL-A','一号')")
        rows = [
            # (line_no, business_day, occurred_at, amount)
            ("CL-1", "2026-11-16", "2026-11-16T09:00:00Z", 100.0),  # 一致
            ("CL-2", "2026-11-16", "2026-11-17T00:30:00Z", 50.0),  # 跨日，可改归
            ("CL-3", "2026-11-15", "2026-11-15T20:00:00Z", 25.0),  # 一致，但该日已封
        ]
        for no, day, occurred, amount in rows:
            conn.execute(
                "INSERT INTO cash_lines(line_no,till_code,business_day,occurred_at,amount,"
                "pay_method,created_at) VALUES(?, 'TILL-A', ?, ?, ?, '现金', 'x')",
                (no, day, occurred, amount),
            )
        conn.execute(
            "INSERT INTO close_batches(batch_no,business_day,system_total,declared_cash,variance,"
            "line_count,status,sealed_at) VALUES('CB-0001','2026-11-15',25,25,0,1,'sealed','x')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _dump(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        lines = {
            r["line_no"]: dict(r)
            for r in conn.execute("SELECT * FROM cash_lines ORDER BY line_no").fetchall()
        }
        corrections = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attribution_corrections ORDER BY id"
            ).fetchall()
        ]
        batches = [dict(r) for r in conn.execute("SELECT * FROM close_batches").fetchall()]
        return lines, corrections, batches
    finally:
        conn.close()


def test_legacy_rows_are_attributed_and_logged(legacy_db):
    import app.seed as seed

    seed.seed_if_empty()
    lines, corrections, batches = _dump(legacy_db)

    # 一致的行原样保留、不留痕
    assert lines["CL-1"]["business_day"] == "2026-11-16"
    # 跨日的行被归入发生日，且写了校正留痕
    assert lines["CL-2"]["business_day"] == "2026-11-17"
    assert len(corrections) == 1
    trail = corrections[0]
    assert trail["line_no"] == "CL-2"
    assert trail["declared_business_day"] == "2026-11-16"
    assert trail["occurred_day"] == "2026-11-17"
    assert trail["stored_business_day"] == "2026-11-17"
    assert trail["action"] == "repair"
    # 已封批次不动
    assert batches[0]["system_total"] == pytest.approx(25.0)

    # 再启动一次不重复改
    seed.seed_if_empty()
    lines2, corrections2, _ = _dump(legacy_db)
    assert lines2 == lines
    assert corrections2 == corrections


def test_legacy_migration_keeps_aggregation_consistent(legacy_db):
    import app.seed as seed
    from app.services import close_service

    seed.seed_if_empty()
    preview_16 = close_service.preview("2026-11-16")
    preview_17 = close_service.preview("2026-11-17")
    assert preview_16["system_total"] == pytest.approx(100.0)
    assert preview_16["line_count"] == 1
    assert preview_17["system_total"] == pytest.approx(50.0)
    assert preview_17["corrected_count"] == 1

    # 迁移后自检无脏行，已封批次合计与落库日流水一致
    integrity = close_service.integrity()
    assert integrity["ok"] is True
    assert integrity["mismatched_line_count"] == 0
