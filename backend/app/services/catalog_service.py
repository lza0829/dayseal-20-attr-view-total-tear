from __future__ import annotations
from typing import Any
from app import attribution
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import align_20 as align_mod


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        raw_day = settings_repo.get(conn, "business_day") or ""
        day = attribution.normalize_day(raw_day) if raw_day else ""
        # 总览与封账预览、批次合计同一归属结果：只按落库 business_day 归集。
        total, count = line_repo.day_total(conn, day) if day else (0.0, 0)
        corrected_total, corrected_count = (
            line_repo.day_corrected_total(conn, day) if day else (0.0, 0)
        )
        sealed = close_repo.is_sealed(conn, day) if day else False
        return {
            "business_day": day,
            "till_count": len(till_repo.list_tills(conn)),
            "line_count_today": count,
            "system_total_today": round(total, 2),
            "corrected_count_today": corrected_count,
            "corrected_total_today": round(corrected_total, 2),
            "sealed_today": sealed,
            "attribution": attribution.describe(),
            "close_batch_count": conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"],
            "open_variance_count": conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


def dashboard_aligned() -> dict:
    raw = dashboard()
    return raw

def dashboard_extended() -> dict:
    from app.db import connect
    from app.repositories import closes as close_repo
    from app import align_20 as align_mod
    base = dashboard()
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        return align_mod.overlay_dashboard(base, batches)
    finally:
        conn.close()

