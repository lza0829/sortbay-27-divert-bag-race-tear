"""回流/入袋争用回归夹具。

对应验收口径：
- 回流抢先入袋被拒：test_divert_first_rejects_bag_and_keeps_count_zero
- 入袋先进回流须带上：test_bag_first_lands_and_divert_explicitly_carries_item
- 半截后袋件数与状态拧花必须回滚：test_bag_half_failure_rolls_back_* /
  test_divert_half_failure_rolls_back_* / test_concurrent_half_bag_rollback_*
- 回执成功却无对应袋件：正常链路必须冲正闭合；残态由对账检测：
  test_reconcile_detects_success_receipt_without_bag_item
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import db
from app.main import app
from app.services.flow import add_to_bag, create_divert


def all_table_snapshot(db_path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        snap = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            snap[table] = list(rows)
        return snap
    finally:
        conn.close()


def test_divert_first_rejects_bag_and_keeps_count_zero(client, routed_scan, open_bag):
    scan_id = routed_scan["id"]
    bag_id = open_bag["id"]

    divert = client.post("/api/回流批次", json={"扫描编号": scan_id})
    assert divert.status_code == 200, divert.text
    assert divert.json()["扫描"]["status"] == "错分回流"

    add = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert add.status_code == 400
    assert "回流已先提交" in add.json()["detail"]

    scan = next(s for s in client.get("/api/扫描").json() if s["id"] == scan_id)
    assert scan["status"] == "错分回流"
    assert scan["bag_id"] is None

    bag = next(b for b in client.get("/api/封袋").json() if b["id"] == bag_id)
    assert bag["件数"] == 0

    receipts = client.get(f"/api/回执?扫描编号={scan_id}").json()
    assert [r for r in receipts if r["kind"] == "入袋" and r["status"] == "有效"] == []

    overview = client.get("/api/总览").json()
    assert overview["对账"]["平衡"] is True
    replay = client.get(f"/api/审计/重放?扫描编号={scan_id}").json()
    assert replay["一致"] is True, replay["差异"]


def test_bag_first_lands_and_divert_explicitly_carries_item(client, routed_scan, open_bag):
    scan_id = routed_scan["id"]
    bag_id = open_bag["id"]

    add = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert add.status_code == 200, add.text
    assert add.json()["status"] == "已入袋"
    assert add.json()["袋件数"] == 1
    bag_receipt_id = add.json()["回执"]["id"]

    divert = client.post("/api/回流批次", json={"扫描编号": scan_id})
    assert divert.status_code == 200, divert.text
    body = divert.json()
    assert body["带出袋件"] is True
    assert body["原袋编号"] == bag_id
    assert body["扫描"]["status"] == "错分回流"
    assert body["扫描"]["bag_id"] is None

    bag_detail = client.get(f"/api/封袋/{bag_id}").json()
    assert bag_detail["件数"] == 0
    reversed_receipt = next(r for r in bag_detail["回执"] if r["id"] == bag_receipt_id)
    assert reversed_receipt["status"] == "已冲正"
    assert reversed_receipt["reversed_by_receipt_id"] == body["回执"]["id"]

    batch = client.get(f"/api/回流批次/{body['批次']['id']}").json()
    assert batch["件数"] == 1
    assert batch["明细"][0]["scan_id"] == scan_id
    assert batch["明细"][0]["from_bag_id"] == bag_id

    audit_actions = {(a["kind"], a["action"]) for a in client.get("/api/审计").json()}
    assert ("封袋", "回流带出袋件") in audit_actions

    overview = client.get("/api/总览").json()
    assert overview["对账"]["平衡"] is True, overview["对账"]["差异"]
    replay = client.get(f"/api/审计/重放?扫描编号={scan_id}").json()
    assert replay["一致"] is True, replay["差异"]
    assert replay["指定扫描"]["status"] == "错分回流"


def test_bag_half_failure_rolls_back_to_pre_race_snapshot(db_path, client, routed_scan, open_bag):
    scan_id = routed_scan["id"]
    bag_id = open_bag["id"]
    before = all_table_snapshot(db_path)

    with pytest.raises(RuntimeError, match="bag_after_scan_update"):
        add_to_bag(bag_id, scan_id, fault="bag_after_scan_update")

    after = all_table_snapshot(db_path)
    assert after == before

    with db(write=False) as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert scan["status"] == "已路由"
        assert scan["bag_id"] is None
        assert scan["version"] == 2  # create=1, route CAS=2; rolled-back bag must not become 3
        assert conn.execute(
            "SELECT COUNT(*) c FROM operation_receipts WHERE scan_id=? AND kind='入袋'",
            (scan_id,),
        ).fetchone()["c"] == 0


def test_divert_half_failure_rolls_back_to_pre_race_snapshot(db_path, client, routed_scan):
    scan_id = routed_scan["id"]
    before = all_table_snapshot(db_path)

    with pytest.raises(RuntimeError, match="divert_after_scan_update"):
        create_divert(scan_id, fault="divert_after_scan_update")

    after = all_table_snapshot(db_path)
    assert after == before

    with db(write=False) as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert scan["status"] == "已路由"
        assert scan["divert_batch_id"] is None
        assert scan["version"] == 2  # rolled-back divert must not add another CAS version
        assert conn.execute(
            "SELECT COUNT(*) c FROM divert_batches"
        ).fetchone()["c"] == len(before["divert_batches"])


def test_http_hold_entry_rolls_back_instead_of_returning_false_success(
    monkeypatch, db_path, client, routed_scan, open_bag
):
    monkeypatch.setenv("SORTBAY_ENABLE_FAULTS", "1")
    sid = routed_scan["id"]
    bid = open_bag["id"]
    before = all_table_snapshot(db_path)

    with TestClient(app, raise_server_exceptions=False) as fault_client:
        resp = fault_client.post(
            f"/api/封袋/{bid}/入袋",
            json={"扫描编号": sid, "故障点": "bag_after_scan_update"},
        )

    assert resp.status_code == 500
    after = all_table_snapshot(db_path)
    assert after == before


def test_reconcile_detects_success_receipt_without_bag_item(client, routed_scan, open_bag, db_path):
    scan_id = routed_scan["id"]
    bag_id = open_bag["id"]
    add = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert add.status_code == 200

    # Simulate the forbidden residual: scan/count moved but receipt was not
    # closed/reversed. Reconciliation must not pretend this is balanced.
    with sqlite3.connect(db_path) as corruption:
        corruption.execute(
            "UPDATE scans SET status='错分回流', bag_id=NULL WHERE id=?",
            (scan_id,),
        )
        corruption.commit()

    rec = client.get("/api/班次/对账").json()
    assert rec["平衡"] is False
    codes = {i["代码"] for i in rec["差异"]}
    assert "BAG_RECEIPT_DANGLING" in codes
    assert "DIVERT_WITHOUT_BATCH" in codes


def test_surface_endpoints_all_replay_same_consistent_state(client, routed_scan, open_bag):
    sid = routed_scan["id"]
    bid = open_bag["id"]
    assert client.post(f"/api/封袋/{bid}/入袋", json={"扫描编号": sid}).status_code == 200
    assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200

    endpoints = [
        "/api/总览",
        "/api/总览/对账",
        "/api/扫描",
        "/api/扫描册",
        "/api/口门",
        "/api/回流批次",
        "/api/封袋",
        f"/api/封袋/{bid}",
        "/api/审计",
        "/api/审计/重放",
        "/api/回执",
        "/api/班次/对账",
    ]
    for endpoint in endpoints:
        resp = client.get(endpoint)
        assert resp.status_code == 200, (endpoint, resp.status_code, resp.text)

    overview = client.get("/api/总览").json()
    assert overview["对账"]["平衡"] is True
    assert client.get("/api/审计/重放").json()["一致"] is True


def test_real_concurrent_divert_and_bag_have_one_replayable_outcome(db_path, client, seeded_ids, open_bag):
    bag_id = open_bag["id"]

    def make_routed(i: int) -> int:
        barcode = f"YB-RACE-{i}-{__import__('os').urandom(3).hex()}"
        created = client.post("/api/扫描", json={"条码": barcode, "目的地码": "110"})
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        routed = client.post(f"/api/扫描/{sid}/路由")
        assert routed.status_code == 200, routed.text
        return sid

    for round_no in range(6):
        sid = make_routed(round_no)
        barrier = threading.Barrier(2)
        outcomes: dict[str, tuple] = {}

        def bag_worker():
            barrier.wait()
            try:
                outcomes["bag"] = ("ok", add_to_bag(bag_id, sid))
            except HTTPException as exc:
                outcomes["bag"] = ("error", exc.status_code, exc.detail)
            except Exception as exc:  # pragma: no cover - failure output
                outcomes["bag"] = ("fatal", type(exc).__name__, str(exc))

        def divert_worker():
            barrier.wait()
            try:
                outcomes["divert"] = ("ok", create_divert(sid))
            except HTTPException as exc:
                outcomes["divert"] = ("error", exc.status_code, exc.detail)
            except Exception as exc:  # pragma: no cover - failure output
                outcomes["divert"] = ("fatal", type(exc).__name__, str(exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(bag_worker)
            f2 = pool.submit(divert_worker)
            f1.result(timeout=30)
            f2.result(timeout=30)

        assert outcomes["divert"][0] == "ok", outcomes
        assert outcomes["bag"][0] in ("ok", "error"), outcomes
        if outcomes["bag"][0] == "error":
            assert outcomes["bag"][1] == 400, outcomes
        assert "fatal" not in outcomes["bag"]
        assert "fatal" not in outcomes["divert"]

        with db(write=False) as conn:
            scan = conn.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
            assert scan["status"] == "错分回流"
            assert scan["bag_id"] is None
            assert scan["divert_batch_id"] is not None
            assert conn.execute(
                "SELECT COUNT(*) c FROM divert_items WHERE scan_id=?", (sid,)
            ).fetchone()["c"] == 1
            assert conn.execute(
                "SELECT COUNT(*) c FROM scans WHERE bag_id=? AND id=?",
                (bag_id, sid),
            ).fetchone()["c"] == 0

        rec = client.get("/api/班次/对账").json()
        assert rec["平衡"] is True, rec["差异"]
        replay = client.get(f"/api/审计/重放?扫描编号={sid}").json()
        assert replay["一致"] is True, replay["差异"]

        if outcomes["bag"][0] == "ok":
            divert_body = outcomes["divert"][1]
            assert divert_body["带出袋件"] is True
            receipt_id = outcomes["bag"][1]["回执"]["id"]
            with db(write=False) as conn:
                receipt = conn.execute(
                    "SELECT * FROM operation_receipts WHERE id=?", (receipt_id,)
                ).fetchone()
                assert receipt["status"] == "已冲正"
                assert receipt["reversed_by_receipt_id"] == divert_body["回执"]["id"]
        else:
            with db(write=False) as conn:
                assert conn.execute(
                    "SELECT COUNT(*) c FROM operation_receipts WHERE kind='入袋' AND scan_id=?",
                    (sid,),
                ).fetchone()["c"] == 0


def test_barcode_route_decision_replays_to_same_chute(client, routed_scan):
    replay = client.get(f"/api/审计/重放?扫描编号={routed_scan['id']}").json()
    assert replay["一致"] is True, replay["差异"]
    assert replay["指定扫描"]["status"] == "已路由"
    assert replay["指定扫描"]["chute_id"] == routed_scan["chute_id"]


def test_concurrent_half_bag_rollback_never_leaves_phantom_bag_item(
    client, routed_scan, open_bag
):
    sid = routed_scan["id"]
    bid = open_bag["id"]
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def half_bag_worker():
        barrier.wait()
        try:
            add_to_bag(bid, sid, fault="bag_after_scan_update")
            outcomes["bag"] = "unexpected-ok"
        except RuntimeError as exc:
            assert "bag_after_scan_update" in str(exc)
            outcomes["bag"] = "half-rolled-back"
        except HTTPException as exc:
            outcomes["bag"] = f"rejected-{exc.status_code}"

    def divert_worker():
        barrier.wait()
        try:
            create_divert(sid)
            outcomes["divert"] = "ok"
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes["divert"] = f"fatal:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(half_bag_worker)
        f2 = pool.submit(divert_worker)
        f1.result(timeout=30)
        f2.result(timeout=30)

    assert outcomes["divert"] == "ok", outcomes
    assert outcomes["bag"] in ("half-rolled-back", "rejected-400"), outcomes

    bag = next(b for b in client.get("/api/封袋").json() if b["id"] == bid)
    assert bag["件数"] == 0
    scan = next(s for s in client.get("/api/扫描册").json() if s["id"] == sid)
    assert scan["status"] == "错分回流"
    assert scan["bag_id"] is None

    overview = client.get("/api/总览").json()
    assert overview["对账"]["平衡"] is True, overview["对账"]["差异"]
    assert overview["对账"]["袋对账"][0]["件数"] == 0 if overview["对账"]["袋对账"] else True
    replay = client.get(f"/api/审计/重放?扫描编号={sid}").json()
    assert replay["一致"] is True, replay["差异"]
