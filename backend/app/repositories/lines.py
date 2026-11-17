from __future__ import annotations
import sqlite3
from typing import Any

#: 归属依据：每次读流水都把落库日、发生日、是否校正一起带出。
#: 列表过滤、封账预览、批次合计都只按 business_day（落库日）归集。
#: 校正按 line_no 关联：line_no 不复用，删除流水后留痕也不会张冠李戴。
_SELECT = '''
SELECT id, line_no, till_code, business_day, occurred_at, amount, pay_method, note,
       created_at, substr(occurred_at, 1, 10) AS occurred_day,
       CASE
         WHEN EXISTS (
           SELECT 1 FROM attribution_corrections c WHERE c.line_no = cash_lines.line_no
         ) THEN 1 ELSE 0
       END AS attribution_corrected
  FROM cash_lines
'''


def _row(r: sqlite3.Row) -> dict[str, Any]:
    item = dict(r)
    item["attribution_corrected"] = bool(item.get("attribution_corrected"))
    return item


def list_lines(conn: sqlite3.Connection, business_day: str | None = None) -> list[dict[str, Any]]:
    if business_day:
        rows = conn.execute(
            _SELECT + " WHERE business_day=? ORDER BY occurred_at, id",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute(
            _SELECT + " ORDER BY business_day DESC, occurred_at, id"
        ).fetchall()
    return [_row(r) for r in rows]


def day_total(conn: sqlite3.Connection, business_day: str) -> tuple[float, int]:
    """营业日合计：唯一依据是落库 business_day，不按 occurred_at 现折。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM cash_lines WHERE business_day=?",
        (business_day,),
    ).fetchone()
    return float(row["s"]), int(row["c"])


def day_corrected_total(conn: sqlite3.Connection, business_day: str) -> tuple[float, int]:
    """当日由跨日改归进来的流水合计 / 笔数（留痕核对用，不另算一套合计）。"""
    row = conn.execute(
        '''SELECT COALESCE(SUM(l.amount),0) s, COUNT(DISTINCT l.id) c
             FROM cash_lines l
            WHERE l.business_day=?
              AND EXISTS (SELECT 1 FROM attribution_corrections c WHERE c.line_no = l.line_no)''',
        (business_day,),
    ).fetchone()
    return float(row["s"]), int(row["c"])


def next_line_no(conn: sqlite3.Connection) -> str:
    """单号单调递增；同时看过留痕表，删除流水后也不复用旧单号。"""
    row = conn.execute(
        '''SELECT MAX(n) n FROM (
             SELECT MAX(CAST(SUBSTR(line_no, 4) AS INTEGER)) n
               FROM cash_lines WHERE line_no LIKE 'CL-%'
             UNION ALL
             SELECT MAX(CAST(SUBSTR(line_no, 4) AS INTEGER)) n
               FROM attribution_corrections WHERE line_no LIKE 'CL-%'
           )'''
    ).fetchone()
    return f"CL-{int(row['n'] or 0) + 1:04d}"


def insert_line(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)''',
        (
            fields["line_no"],
            fields["till_code"],
            fields["business_day"],
            fields["occurred_at"],
            fields["amount"],
            fields["pay_method"],
            fields.get("note"),
            fields["created_at"],
        ),
    )
    return int(cur.lastrowid)


def update_line(conn: sqlite3.Connection, line_id: int, **fields: Any) -> None:
    conn.execute(
        '''UPDATE cash_lines
              SET amount=?, pay_method=?, note=?, occurred_at=?, business_day=?
            WHERE id=?''',
        (
            fields["amount"],
            fields["pay_method"],
            fields.get("note"),
            fields["occurred_at"],
            fields["business_day"],
            line_id,
        ),
    )


def delete_line(conn: sqlite3.Connection, line_id: int) -> None:
    conn.execute("DELETE FROM cash_lines WHERE id=?", (line_id,))


def get_line(conn: sqlite3.Connection, line_id: int) -> dict[str, Any] | None:
    row = conn.execute(_SELECT + " WHERE id=?", (line_id,)).fetchone()
    return _row(row) if row else None


def mismatched_lines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """库中 business_day 与 occurred_at 发生日不一致的脏行（应为空）。"""
    rows = conn.execute(
        '''SELECT id, line_no, business_day, occurred_at,
                  substr(occurred_at, 1, 10) AS occurred_day
             FROM cash_lines
            WHERE business_day <> substr(occurred_at, 1, 10)
            ORDER BY business_day, id'''
    ).fetchall()
    return [dict(r) for r in rows]

def list_lines_by_declared(conn, declared_day: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM sale_lines ORDER BY id").fetchall()]
    out = []
    for r in rows:
        note = str(r.get("note") or "")
        if r.get("business_day") == declared_day or f"declared={declared_day}" in note:
            r = dict(r)
            r["declared_day"] = declared_day
            out.append(r)
    return out

