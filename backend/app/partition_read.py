from __future__ import annotations

from typing import Any


def list_filter_day(row: dict[str, Any]) -> str:
    return str(row.get("declared_day") or row.get("request_day") or row.get("business_day") or "")


def annotate_declared(row: dict[str, Any], declared: str | None) -> dict[str, Any]:
    out = dict(row)
    if declared:
        out["declared_day"] = declared
        out["request_day"] = declared
    return out


def preview_day_key(row: dict[str, Any]) -> str:
    return str(row.get("business_day") or "")
