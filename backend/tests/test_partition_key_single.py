"""分区键唯一性：落库 business_day 是唯一分区键。

本文件专门钉三类撕裂路径，每条都必须被读路径挡在「一种口径」之内：

1. **双日各计** —— 列表按声明日、预览按落库日，同一条 line 两日各计一次；
2. **两日都不计** —— 两套键互相抵消，同一条 line 哪一日都不计；
3. **留痕与落库列不符** —— 留痕写了新分区，落库列没改（或相反）。

另钉两条铁律：矛盾行不得进新封；已 sealed 分区的合计不得被归属修复撬动。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

DAY = "2026-11-16"
NEXT = "2026-11-17"


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


def _lines(client, day):
    r = client.get(f"/api/lines?business_day={day}")
    assert r.status_code == 200, r.text
    return r.json()


def _preview(client, day):
    r = client.get(f"/api/close/preview?business_day={day}")
    assert r.status_code == 200, r.text
    return r.json()


def _insert_raw(*, business_day, occurred_at, amount=99.0, line_no="CL-9001", note=None):
    """绕过服务层与触发器直接落一行「落库日 ≠ 发生日」的矛盾行。

    触发器是刻意绕过的：它证明服务层挡得住脏写，而本文件的场景是历史脏数据。
    """
    from app.db import connect

    conn = connect(immediate=True)
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_cash_lines_attribution_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_cash_lines_attribution_update")
        conn.execute(
            "INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, "
            "amount, pay_method, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (line_no, "TILL-A", business_day, occurred_at, amount, "现金", note, "x"),
        )
        conn.commit()
    finally:
        conn.close()


def _add_trail(*, line_no, declared, occurred, stored, action="repair"):
    from app.db import connect

    conn = connect(immediate=True)
    try:
        row = conn.execute(
            "SELECT id FROM cash_lines WHERE line_no=?", (line_no,)
        ).fetchone()
        conn.execute(
            "INSERT INTO attribution_corrections(line_id, line_no, action, "
            "declared_business_day, occurred_day, stored_business_day, occurred_at, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], line_no, action, declared, occurred, stored, "x", "测试", "x"),
        )
        conn.commit()
    finally:
        conn.close()


def test_no_second_partition_key_module_on_read_path():
    """读路径不允许存在「先看声明日再看落库日」的第二套键。"""
    import app.services.close_service as close_service
    import app.services.line_service as line_service

    for mod in (close_service, line_service):
        assert not hasattr(mod, "partition_read")


def test_list_and_preview_agree_on_stored_day_for_dirty_row(client):
    """双日各计：矛盾行只认落库日，绝不在声明日/发生日再计一次。"""
    before = {d: _preview(client, d) for d in (DAY, NEXT)}
    _insert_raw(business_day=NEXT, occurred_at=f"{DAY}T23:40:00Z", amount=77.0)

    after = {d: _preview(client, d) for d in (DAY, NEXT)}
    # 落库日 17 认这一行
    assert after[NEXT]["system_total"] == pytest.approx(before[NEXT]["system_total"] + 77.0)
    assert after[NEXT]["line_count"] == before[NEXT]["line_count"] + 1
    # 发生日 16 不另计一次
    assert after[DAY]["system_total"] == pytest.approx(before[DAY]["system_total"])
    assert after[DAY]["line_count"] == before[DAY]["line_count"]

    # 列表过滤与预览逐笔同源
    for day in (DAY, NEXT):
        assert [r["id"] for r in _lines(client, day)] == [
            r["id"] for r in _preview(client, day)["lines"]
        ]
        assert _preview(client, day)["system_total"] == pytest.approx(
            round(sum(r["amount"] for r in _lines(client, day)), 2)
        )


def test_dirty_row_never_falls_out_of_both_days(client):
    """两日都不计：落库日必须认领它；发生日只能「提示未计入」，绝不静默。"""
    _insert_raw(business_day=NEXT, occurred_at=f"{DAY}T23:40:00Z", amount=41.0)
    p17 = _preview(client, NEXT)
    assert any(r["amount"] == pytest.approx(41.0) for r in p17["lines"])
    assert p17["conflicting_count"] == 1
    assert p17["conflicting_lines"][0]["occurred_day"] == DAY
    assert p17["sealable"] is False
    assert p17["aggregation_basis"] == "stored_business_day"

    p16 = _preview(client, DAY)
    # 发生日一侧不认领它（不双计），但必须可见地说明「未计入」
    assert p16["system_total"] == pytest.approx(
        round(sum(r["amount"] for r in _lines(client, DAY)), 2)
    )
    assert p16["incoming_conflicting_count"] == 1
    assert p16["incoming_conflicting_lines"][0]["business_day"] == NEXT


def test_conflicting_row_blocks_new_seal(client):
    """矛盾行必须暴露且不得进新封。"""
    _insert_raw(business_day=NEXT, occurred_at=f"{DAY}T23:40:00Z", amount=41.0)
    r = client.post("/api/close/seal", json={"business_day": NEXT, "declared_cash": 0})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "attribution_conflict"
    assert detail["conflicting_lines"][0]["line_no"] == "CL-9001"
    # 没有写出批次
    assert client.get("/api/close/batches").json() == []

    # 无矛盾行的一日照常可封
    ok = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 0})
    assert ok.status_code == 200, ok.text


def test_trail_pointing_at_other_day_is_reported(client):
    """留痕与落库列不符（留痕指发生日、落库列仍是旧日）。"""
    _insert_raw(business_day=NEXT, occurred_at=f"{DAY}T23:40:00Z", amount=41.0)
    _add_trail(line_no="CL-9001", declared=DAY, occurred=DAY, stored=DAY)

    body = client.get("/api/close/integrity").json()
    assert body["ok"] is False
    assert body["trail_mismatch_count"] == 1
    item = body["trail_mismatches"][0]
    assert item["line_no"] == "CL-9001"
    assert item["live_business_day"] == NEXT
    assert item["stored_business_day"] == DAY
    assert "trail_stored_mismatch" in item["problems"]
    # 落库列与发生日不符也被单列
    assert body["mismatched_line_count"] == 1


def test_trail_internally_inconsistent_is_reported(client):
    """留痕自身的 occurred_day 与 stored_business_day 打架也不能静默。"""
    client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 5.0,
            "business_day": DAY,
            "occurred_at": f"{NEXT}T00:30:00Z",
        },
    )
    from app.db import connect

    conn = connect(immediate=True)
    try:
        conn.execute(
            "UPDATE attribution_corrections SET occurred_day=? WHERE line_no='CL-0007'",
            (DAY,),
        )
        conn.commit()
    finally:
        conn.close()

    body = client.get("/api/close/integrity").json()
    assert body["trail_mismatch_count"] == 1
    assert "trail_occurred_mismatch" in body["trail_mismatches"][0]["problems"]


def test_sealed_batch_totals_survive_later_repair_attempts(client):
    """已 sealed 分区合计不得被归属修复撬动。"""
    client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 20.0,
            "business_day": DAY,
            "occurred_at": f"{DAY}T10:00:00Z",
        },
    )
    sealed = client.post(
        "/api/close/seal", json={"business_day": DAY, "declared_cash": 20.0}
    )
    assert sealed.status_code == 200, sealed.text
    frozen_total = sealed.json()["system_total"]
    frozen_count = sealed.json()["line_count"]

    # 往已封日塞一行矛盾数据，再跑一次迁移：已封分区不动，批次合计不变。
    _insert_raw(
        business_day=DAY, occurred_at=f"{NEXT}T01:00:00Z", amount=5.0, line_no="CL-9101"
    )
    import app.seed as seed

    seed.seed_if_empty()

    from app.db import connect

    conn = connect()
    try:
        row = conn.execute(
            "SELECT business_day FROM cash_lines WHERE line_no='CL-9101'"
        ).fetchone()
    finally:
        conn.close()
    assert row["business_day"] == DAY  # 未被动迁移

    batch = client.get("/api/close/batches").json()[0]
    assert batch["system_total"] == pytest.approx(frozen_total)
    assert batch["line_count"] == frozen_count

    # 矛盾仍在，自检必须报出来而不是假装 ok
    body = client.get("/api/close/integrity").json()
    assert body["ok"] is False
    assert body["mismatched_line_count"] == 1
    # 且不能再次封同一天
    again = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 0})
    assert again.status_code == 409


def test_created_line_note_does_not_carry_declared_day(client):
    """声明日只进审计附件，不得被写进 note 供读路径二次分区。"""
    r = client.post(
        "/api/lines",
        json={
            "till_code": "TILL-A",
            "amount": 9.0,
            "business_day": DAY,
            "occurred_at": f"{NEXT}T00:05:00Z",
            "note": "夜班",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "夜班"
