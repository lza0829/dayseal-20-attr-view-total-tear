"""流水营业日归属：一致成功、跨日强制改归、两日合计加减、已封日拒绝改归属。

全部按落库 business_day 核对：列表过滤、封账预览、批次合计必须是同一个数。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.db as db
    import app.seed as seed

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "dayseal.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "dayseal.db")

    seed.seed_if_empty()

    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


DAY = "2026-11-16"
NEXT = "2026-11-17"


def _lines(client, day=DAY):
    r = client.get(f"/api/lines?business_day={day}")
    assert r.status_code == 200, r.text
    return r.json()


def _preview(client, day=DAY):
    r = client.get(f"/api/close/preview?business_day={day}")
    assert r.status_code == 200, r.text
    return r.json()


def _corrections(client, day=None):
    url = "/api/lines/corrections"
    if day:
        url += f"?business_day={day}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def _lines_total(client, day):
    return round(sum(r["amount"] for r in _lines(client, day)), 2)


def test_policy_is_single_and_declared(client):
    body = client.get("/api/lines/attribution").json()
    assert body["policy"] == "force_occurrence_day"
    assert body["aggregation_basis"] == "stored_business_day"
    # 另一策略明确标注未实现，不提供切换开关
    assert [a["policy"] for a in body["alternatives"]] == ["reject"]
    assert body["alternatives"][0]["implemented"] is False
    assert "close_scope" not in str(body)


def test_consistent_day_succeeds_without_correction(client):
    before = _preview(client)
    r = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 10.0,
            "business_day": DAY,
            "occurred_at": f"{DAY}T20:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    line = r.json()
    assert line["business_day"] == DAY
    assert line["declared_business_day"] == DAY
    assert line["attribution_corrected"] is False

    after = _preview(client)
    assert after["line_count"] == before["line_count"] + 1
    assert after["system_total"] == pytest.approx(before["system_total"] + 10.0)
    assert _corrections(client) == []


def test_cross_day_is_forced_into_occurrence_day_with_trail(client):
    before_16 = _preview(client, DAY)
    before_17 = _preview(client, NEXT)

    r = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 5.0,
            "business_day": DAY,
            "occurred_at": f"{NEXT}T00:30:00Z",
        },
    )
    assert r.status_code == 200, r.text
    line = r.json()
    assert line["business_day"] == NEXT  # 落库以发生日为准
    assert line["declared_business_day"] == DAY  # 声明日只留痕
    assert line["attribution_corrected"] is True
    assert line["occurred_day"] == NEXT

    after_16 = _preview(client, DAY)
    after_17 = _preview(client, NEXT)
    assert after_16["system_total"] == pytest.approx(before_16["system_total"])
    assert after_16["line_count"] == before_16["line_count"]
    assert after_17["system_total"] == pytest.approx(before_17["system_total"] + 5.0)
    assert after_17["line_count"] == before_17["line_count"] + 1

    # 列表过滤与预览同源
    assert len(_lines(client, DAY)) == after_16["line_count"]
    assert len(_lines(client, NEXT)) == after_17["line_count"]

    trail = _corrections(client, NEXT)
    assert len(trail) == 1
    assert trail[0]["declared_business_day"] == DAY
    assert trail[0]["occurred_day"] == NEXT
    assert trail[0]["stored_business_day"] == NEXT
    assert trail[0]["line_id"] == line["id"]


def test_two_day_preview_totals_move_together(client):
    """改归后两日预览合计加减正确，且与列表、批次合计同源。"""
    line = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-B",
            "amount": 12.5,
            "business_day": DAY,
            "occurred_at": f"{DAY}T23:50:00Z",
        },
    ).json()
    s16 = _preview(client, DAY)["system_total"]
    s17 = _preview(client, NEXT)["system_total"]

    # 把发生时刻改到次日：同一笔从 16 日挪到 17 日
    moved = client.patch(f"/api/lines/{line['id']}", json={"occurred_at": f"{NEXT}T00:10:00Z"})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["business_day"] == NEXT
    assert body["declared_business_day"] == DAY
    assert body["attribution_corrected"] is True

    assert _preview(client, DAY)["system_total"] == pytest.approx(s16 - 12.5)
    assert _preview(client, NEXT)["system_total"] == pytest.approx(s17 + 12.5)
    assert line["id"] not in [r["id"] for r in _lines(client, DAY)]
    assert line["id"] in [r["id"] for r in _lines(client, NEXT)]


def test_import_is_atomic_and_multi_day(client):
    count16 = _preview(client, DAY)["line_count"]
    r = client.post(
        "/api/lines/import",
        json={
            "business_day": DAY,
            "rows": [
                {"till_code": "TILL-A", "amount": 10.0, "occurred_at": f"{DAY}T10:00:00Z"},
                {
                    "till_code": "TILL-B",
                    "amount": 20.0,
                    "business_day": DAY,
                    "occurred_at": f"{NEXT}T01:00:00Z",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 2
    assert body["corrections"] == 1
    days = {d["business_day"]: d for d in body["days"]}
    assert days[DAY]["system_total"] == pytest.approx(10.0)
    assert days[NEXT]["system_total"] == pytest.approx(20.0)

    # 合计与预览同源
    assert _preview(client, DAY)["line_count"] == count16 + 1
    preview17 = _preview(client, NEXT)
    assert preview17["system_total"] == pytest.approx(
        _lines_total(client, NEXT)
    )
    assert preview17["corrected_total"] == pytest.approx(20.0)

    # 坏行 → 整批不落库
    count_before = _preview(client, DAY)["line_count"]
    bad = client.post(
        "/api/lines/import",
        json={
            "rows": [
                {"till_code": "TILL-A", "amount": 10.0, "occurred_at": f"{DAY}T10:00:00Z"},
                {"till_code": "TILL-B", "amount": 20.0, "occurred_at": "not-a-date"},
            ]
        },
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["row"] == 2
    assert _preview(client, DAY)["line_count"] == count_before


def test_sealed_day_rejects_re_attribution(client):
    r = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 7.0,
            "business_day": DAY,
            "occurred_at": f"{DAY}T09:00:00Z",
        },
    )
    line_id = r.json()["id"]

    sealed = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 7.0})
    assert sealed.status_code == 200, sealed.text
    sealed_total = sealed.json()["system_total"]
    sealed_count = sealed.json()["line_count"]

    # 改归属 / 改金额 / 新增 全部 409
    for patch in (
        {"occurred_at": f"{NEXT}T00:01:00Z"},
        {"business_day": NEXT},
        {"amount": 1.0},
    ):
        resp = client.patch(f"/api/lines/{line_id}", json=patch)
        assert resp.status_code == 409, resp.text
    new_line = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 1.0,
            "business_day": DAY,
            "occurred_at": f"{DAY}T10:00:00Z",
        },
    )
    assert new_line.status_code == 409
    # 想改归到已封日也拒绝（从次日挪进来）
    other = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 2.0,
            "business_day": NEXT,
            "occurred_at": f"{NEXT}T10:00:00Z",
        },
    ).json()
    resp = client.patch(f"/api/lines/{other['id']}", json={"occurred_at": f"{DAY}T10:00:00Z"})
    assert resp.status_code == 409

    # 已封批次数字与落库日流水合计仍一致
    batch = client.get("/api/close/batches").json()[0]
    assert batch["system_total"] == pytest.approx(sealed_total)
    assert batch["line_count"] == sealed_count
    assert client.get("/api/close/integrity").json()["ok"] is True


def test_db_level_guard_blocks_dirty_rows(client):
    """绕过服务层直接写库也写不出 business_day != 发生日 的行。"""
    from app.db import connect

    conn = connect(immediate=True)
    try:
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
                "amount, pay_method, created_at) VALUES ('CL-X','TILL-A',?,?,1,'现金','x')",
                (DAY, f"{NEXT}T01:00:00Z"),
            )
    finally:
        conn.close()

    assert client.get("/api/close/integrity").json()["mismatched_line_count"] == 0


def test_dashboard_uses_stored_day(client):
    client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 3.0,
            "business_day": DAY,
            "occurred_at": f"{NEXT}T00:05:00Z",
        },
    )
    dash = client.get("/api/dashboard").json()
    preview = _preview(client, DAY)
    assert dash["system_total_today"] == pytest.approx(preview["system_total"])
    assert dash["line_count_today"] == preview["line_count"]
    assert dash["attribution"]["policy"] == "force_occurrence_day"
