from __future__ import annotations
import sqlite3
from typing import Any


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, system_total, declared_cash, variance,
             line_count, status, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["system_total"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["status"],
            fields["sealed_at"],
            fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at)
           VALUES (?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["variance"],
            fields["disposition"],
            fields["created_at"],
        ),
    )


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]


def add_correction(conn: sqlite3.Connection, **fields: Any) -> int:
    """写归属校正留痕；只在发生日覆盖声明日时调用。"""
    cur = conn.execute(
        '''INSERT INTO attribution_corrections(
             line_id, line_no, action, declared_business_day, occurred_day,
             stored_business_day, occurred_at, reason, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            fields["line_id"],
            fields["line_no"],
            fields["action"],
            fields["declared_business_day"],
            fields["occurred_day"],
            fields["stored_business_day"],
            fields["occurred_at"],
            fields.get("reason"),
            fields["created_at"],
        ),
    )
    return int(cur.lastrowid)


def list_corrections(
    conn: sqlite3.Connection, business_day: str | None = None
) -> list[dict[str, Any]]:
    """归属校正留痕；``business_day`` 过滤的是落库日，与合计同源。"""
    if business_day:
        rows = conn.execute(
            "SELECT * FROM attribution_corrections WHERE stored_business_day=? "
            "ORDER BY id DESC",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM attribution_corrections ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def trail_mismatches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """留痕与落库列不符的流水。

    两种方向都要报，否则「留痕写了新分区、落库列没改」或「落库列挪了、留痕还指旧日」
    都会退化成静默分裂：

    * ``trail_stored_mismatch``：留痕的 ``stored_business_day`` 不等于该行当前落库日；
    * ``trail_occurred_mismatch``：留痕的 ``occurred_day`` 与 ``stored_business_day``
      自相矛盾（现行策略下两者必然相等）。

    删除流水的留痕没有对应行，不算不符——留痕按 line_no 保留，line_no 不复用。
    """
    rows = conn.execute(
        '''SELECT c.id, c.line_id, c.line_no, c.action, c.declared_business_day,
                  c.occurred_day, c.stored_business_day, c.created_at,
                  l.id AS live_id, l.business_day AS live_business_day,
                  substr(l.occurred_at, 1, 10) AS live_occurred_day
             FROM attribution_corrections c
             LEFT JOIN cash_lines l ON l.line_no = c.line_no
            ORDER BY c.id'''
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        if item["live_id"] is None:
            continue
        problems = []
        if item["stored_business_day"] != item["live_business_day"]:
            problems.append("trail_stored_mismatch")
        if item["occurred_day"] != item["stored_business_day"]:
            problems.append("trail_occurred_mismatch")
        if problems:
            item["problems"] = problems
            out.append(item)
    return out

