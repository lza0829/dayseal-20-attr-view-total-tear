from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app import attribution
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import align_20 as align_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _attribution_conflict(conn: sqlite3.Connection, exc: sqlite3.IntegrityError) -> HTTPException | None:
    """触发器拦下的归属/封账写入 → 409；其他约束错误返回 None 由调用方重抛。"""
    conn.rollback()
    text = str(exc)
    if "发生日" in text:
        return HTTPException(
            409,
            {
                "code": "attribution_violation",
                "message": "落库营业日必须以 occurred_at 的发生日为准，已拒绝写入",
            },
        )
    if "封账" in text:
        return HTTPException(409, {"code": "sealed_day", "message": "营业日已封账，禁止改归属"})
    return None


def _write_correction(
    conn: sqlite3.Connection,
    line: dict[str, Any],
    resolution: attribution.Resolution,
    *,
    action: str,
    created_at: str,
) -> None:
    """发生日覆盖了声明日才写留痕；一致时不留痕。"""
    if not resolution.corrected:
        return
    close_repo.add_correction(
        conn,
        line_id=line["id"],
        line_no=line["line_no"],
        action=action,
        declared_business_day=resolution.declared_business_day,
        occurred_day=resolution.occurred_day,
        stored_business_day=resolution.business_day,
        occurred_at=resolution.occurred_at,
        reason=resolution.reason,
        created_at=created_at,
    )


def list_lines(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        day = attribution.normalize_day(business_day) if business_day else None
        return line_repo.list_lines(conn, day)
    finally:
        conn.close()


def list_corrections(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        day = attribution.normalize_day(business_day) if business_day else None
        return close_repo.list_corrections(conn, day)
    finally:
        conn.close()


def _require(body: dict[str, Any], key: str) -> Any:
    if body.get(key) is None or (isinstance(body.get(key), str) and not body[key].strip()):
        raise HTTPException(400, f"{key} 不能为空")
    return body[key]


def create_line(body: dict[str, Any]) -> dict[str, Any]:
    """登记流水：落库日 = 发生日；声明日不同只写校正留痕，不改任何合计口径。"""
    conn = connect(immediate=True)
    try:
        till_code = _require(body, "till_code")
        amount = _require(body, "amount")
        declared = body.get("business_day")
        if declared is None or str(declared).strip() == "":
            declared = settings_repo.get(conn, "business_day")
        resolution = attribution.resolve(
            declared,
            body.get("occurred_at"),
            timestamp_missing="occurred_at" not in body or body.get("occurred_at") is None,
        )
        day = resolution.business_day
        # 封账闸门只看落库日（发生日）；声明日只进校正留痕，不做任何判定依据。
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账，禁止新增流水")
        created = _now()
        lid = line_repo.insert_line(
            conn,
            line_no=line_repo.next_line_no(conn),
            till_code=till_code,
            business_day=day,
            occurred_at=resolution.occurred_at,
            amount=float(amount),
            pay_method=body.get("pay_method") or "现金",
            note=body.get("note"),
            created_at=created,
        )
        line = line_repo.get_line(conn, lid)
        _write_correction(
            conn, line, resolution, action=attribution.ACTION_CREATE, created_at=created
        )
        conn.commit()
        return {**line, **resolution.as_meta()}  # type: ignore[dict-item]
    except sqlite3.IntegrityError as exc:
        conflict = _attribution_conflict(conn, exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def patch_line(line_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """修改流水：发生时刻变了就按新发生日重新归属；已封日拒绝再改归属。"""
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(
                409, f"营业日 {row['business_day']} 已封账，禁止修改流水或改归属"
            )

        occurred_raw = body.get("occurred_at")
        occurred_given = occurred_raw is not None and str(occurred_raw).strip() != ""
        occurred_text = str(occurred_raw).strip() if occurred_given else row["occurred_at"]
        declared = body.get("business_day")
        if declared is None or str(declared).strip() == "":
            # 没声明：声明日即流水当前落库日；发生时刻若跨日，就是一次归属校正。
            declared = row["business_day"]
        resolution = attribution.resolve(declared, occurred_text, timestamp_missing=False)

        if resolution.business_day != row["business_day"]:
            if close_repo.is_sealed(conn, resolution.business_day):
                raise HTTPException(
                    409,
                    f"营业日 {resolution.business_day} 已封账，禁止把流水改归属到该日",
                )
        amount = body.get("amount")
        line_repo.update_line(
            conn,
            line_id,
            amount=float(row["amount"] if amount is None else amount),
            pay_method=body.get("pay_method", row["pay_method"]),
            note=body.get("note", row["note"]),
            occurred_at=resolution.occurred_at,
            business_day=resolution.business_day,
        )
        line = line_repo.get_line(conn, line_id)
        _write_correction(
            conn, line, resolution, action=attribution.ACTION_PATCH, created_at=_now()
        )
        conn.commit()
        return {**line, **resolution.as_meta()}  # type: ignore[dict-item]
    except sqlite3.IntegrityError as exc:
        conflict = _attribution_conflict(conn, exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_lines(body: dict[str, Any]) -> dict[str, Any]:
    """批量导入：与登记同一归属裁决，整批一个事务，任一校验失败整批不落库。

    每行可用自己的 ``business_day`` / ``occurred_at``；缺省时用批次头。
    导入成功后按**落库 business_day** 汇总本批合计（与封账预览同源）。
    """
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "rows 不能为空，需为流水数组")
    dry_run = bool(body.get("dry_run"))

    conn = connect(immediate=True)
    try:
        header_day = body.get("business_day")
        if header_day is None or str(header_day).strip() == "":
            header_day = settings_repo.get(conn, "business_day")
        header_day = attribution.normalize_day(header_day) if header_day else None

        created = _now()
        imported: list[dict[str, Any]] = []
        corrections = 0
        days: dict[str, dict[str, Any]] = {}

        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise HTTPException(400, f"第 {index + 1} 行不是对象")
            declared = raw.get("business_day") or header_day
            occurred_given = raw.get("occurred_at") is not None and str(
                raw.get("occurred_at")
            ).strip() != ""
            try:
                resolution = attribution.resolve(
                    declared, raw.get("occurred_at"), timestamp_missing=not occurred_given
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
                raise HTTPException(400, {**detail, "row": index + 1}) from exc

            day = resolution.business_day
            if close_repo.is_sealed(conn, day):
                raise HTTPException(
                    409,
                    {
                        "code": "sealed_day",
                        "message": f"营业日 {day} 已封账，禁止导入流水",
                        "row": index + 1,
                    },
                )
            if raw.get("till_code") is None:
                raise HTTPException(400, {"message": "till_code 不能为空", "row": index + 1})
            if raw.get("amount") is None:
                raise HTTPException(400, {"message": "amount 不能为空", "row": index + 1})

            lid = line_repo.insert_line(
                conn,
                line_no=line_repo.next_line_no(conn),
                till_code=raw["till_code"],
                business_day=day,
                occurred_at=resolution.occurred_at,
                amount=float(raw["amount"]),
                pay_method=raw.get("pay_method") or "现金",
                note=raw.get("note"),
                created_at=created,
            )
            line = line_repo.get_line(conn, lid)
            _write_correction(
                conn,
                line,
                resolution,
                action=attribution.ACTION_IMPORT,
                created_at=created,
            )
            corrections += 1 if resolution.corrected else 0
            imported.append(line)
            bucket = days.setdefault(
                day, {"business_day": day, "line_count": 0, "system_total": 0.0}
            )
            bucket["line_count"] += 1
            bucket["system_total"] = round(
                bucket["system_total"] + float(raw["amount"]), 2
            )

        if dry_run:
            conn.rollback()
            return {
                "dry_run": True,
                "imported": len(imported),
                "corrections": corrections,
                "days": sorted(days.values(), key=lambda d: d["business_day"]),
                "lines": imported,
                "attribution": attribution.describe(),
            }

        conn.commit()
        return {
            "dry_run": False,
            "imported": len(imported),
            "corrections": corrections,
            "days": sorted(days.values(), key=lambda d: d["business_day"]),
            "lines": imported,
            "attribution": attribution.describe(),
        }
    except sqlite3.IntegrityError as exc:
        conflict = _attribution_conflict(conn, exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_line(line_id: int) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(
                409, f"营业日 {row['business_day']} 已封账，禁止删除流水或改归属"
            )
        line_repo.delete_line(conn, line_id)
        conn.commit()
        return {"deleted": True, "id": line_id, "business_day": row["business_day"]}
    except sqlite3.IntegrityError as exc:
        conflict = _attribution_conflict(conn, exc)
        if conflict:
            raise conflict from exc
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_lines_export(business_day: str | None = None) -> list[dict]:
    rows = list_lines(business_day)
    return align_mod.filter_lines_for_export(rows)


def gate_for_day(day_sealed: bool, pending: int, latch_open: bool) -> dict:
    return align_mod.gate_matrix(day_sealed, pending, latch_open)

