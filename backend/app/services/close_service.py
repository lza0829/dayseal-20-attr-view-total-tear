from __future__ import annotations
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


def preview(business_day: str | None = None) -> dict[str, Any]:
    """封账预览：只按落库 business_day 归集，与列表过滤、批次合计同一归属结果。

    声明日不参与任何过滤或求和；矛盾行（落库日 ≠ 发生日）单独列出并阻止封账，
    不允许它在两种日期口径下被计两次或两次都不计。
    """
    conn = connect()
    try:
        day = attribution.normalize_day(
            business_day or settings_repo.get(conn, "business_day") or ""
        )
        total, count = line_repo.day_total(conn, day)
        corrected_total, corrected_count = line_repo.day_corrected_total(conn, day)
        conflicts = line_repo.conflicting_lines(conn, day)
        incoming = line_repo.incoming_conflicting_lines(conn, day)
        return {
            "business_day": day,
            "system_total": round(total, 2),
            "line_count": count,
            "corrected_total": round(corrected_total, 2),
            "corrected_count": corrected_count,
            "sealed": close_repo.is_sealed(conn, day),
            "sealable": not conflicts,
            "conflicting_count": len(conflicts),
            "conflicting_lines": conflicts,
            "incoming_conflicting_count": len(incoming),
            "incoming_conflicting_lines": incoming,
            "aggregation_basis": attribution.AGGREGATION_BASIS,
            "lines": line_repo.list_lines(conn, day),
            "attribution": attribution.describe(),
        }
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        raw_day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not raw_day:
            raise HTTPException(400, "未指定营业日")
        day = attribution.normalize_day(raw_day)
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账")
        # 矛盾行不得进新封：否则批次合计与实际发生日归属永久打架，
        # 事后归属修复又会撬动已 sealed 的合计。
        conflicts = line_repo.conflicting_lines(conn, day)
        if conflicts:
            raise HTTPException(
                409,
                {
                    "code": "attribution_conflict",
                    "message": (
                        f"营业日 {day} 存在 {len(conflicts)} 笔落库日与发生日不一致的流水，"
                        "禁止封账；请先归位或移出该日"
                    ),
                    "business_day": day,
                    "conflicting_lines": conflicts,
                },
            )
        declared = float(body["declared_cash"])
        # 批次合计与预览同一归属结果：只按落库 business_day 求和。
        total, count = line_repo.day_total(conn, day)
        variance = round(declared - total, 2)
        sealed_at = _now()
        corrected_total, corrected_count = line_repo.day_corrected_total(conn, day)
        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            system_total=total,
            declared_cash=declared,
            variance=variance,
            line_count=count,
            status="sealed",
            sealed_at=sealed_at,
            note=body.get("note"),
        )
        disposition = "pending" if abs(variance) > 1e-9 else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "system_total": total,
            "declared_cash": declared,
            "variance": variance,
            "line_count": count,
            "corrected_total": round(corrected_total, 2),
            "corrected_count": corrected_count,
            "status": "sealed",
            "disposition": disposition,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def integrity() -> dict[str, Any]:
    """归属自检：落库日 == 发生日；留痕与落库列相符；批次合计 == 落库日流水合计。"""
    conn = connect()
    try:
        mismatched = line_repo.mismatched_lines(conn)
        trail = close_repo.trail_mismatches(conn)
        batches = close_repo.list_batches(conn)
        drift = []
        for batch in batches:
            if batch["status"] != "sealed":
                continue
            total, count = line_repo.day_total(conn, batch["business_day"])
            if abs(total - batch["system_total"]) > 1e-9 or count != batch["line_count"]:
                drift.append(
                    {
                        "batch_no": batch["batch_no"],
                        "business_day": batch["business_day"],
                        "batch_total": batch["system_total"],
                        "batch_line_count": batch["line_count"],
                        "stored_total": round(total, 2),
                        "stored_line_count": count,
                    }
                )
        return {
            "ok": not mismatched and not drift and not trail,
            "aggregation_basis": attribution.AGGREGATION_BASIS,
            "attribution": attribution.describe(),
            "mismatched_line_count": len(mismatched),
            "mismatched_lines": mismatched,
            "trail_mismatch_count": len(trail),
            "trail_mismatches": trail,
            "batch_drift_count": len(drift),
            "batch_drift": drift,
        }
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_batches(conn)
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_variances(conn)
    finally:
        conn.close()

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

