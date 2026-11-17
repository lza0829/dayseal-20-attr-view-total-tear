from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app import attribution
from app.db import connect
from app.repositories import settings as settings_repo


def get_day() -> dict[str, Any]:
    conn = connect()
    try:
        return {"business_day": settings_repo.get(conn, "business_day")}
    finally:
        conn.close()


def set_day(body: dict[str, Any]) -> dict[str, Any]:
    day = attribution.normalize_day(body.get("business_day"), "business_day")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "business_day", day)
        conn.commit()
        return {"business_day": day}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
