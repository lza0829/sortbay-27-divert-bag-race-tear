from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import db
from app.main import app
from app.services.flow import add_to_bag, create_divert


def test_truthful_lights_and_scan_audit_query(client, routed_scan, open_bag):
    sid = routed_scan["id"]
    bid = open_bag["id"]

    bag = client.post(f"/api/封袋/{bid}/入袋", json={"扫描编号": sid}).json()
    assert bag["已入袋"] is True
    assert bag["已回流"] is False
    assert bag["dual_success"] is False

    divert = client.post("/api/回流批次", json={"扫描编号": sid}).json()
    assert divert["已回流"] is True
    assert divert["已入袋"] is False
    assert divert["dual_success"] is False

    scan = client.get(f"/api/扫描/{sid}").json()
    assert scan["已回流"] is True
    assert scan["已入袋"] is False
    assert scan["成功灯"] == {"回流": True, "集包": False}

    audits = client.get(f"/api/审计?扫描编号={sid}").json()
    actions = {(a["kind"], a["action"]) for a in audits}
    assert ("扫描", "路由裁决") in actions
    assert ("封袋", "入袋") in actions
    assert ("回流", "建回流批次") in actions
    route_audit = next(a for a in audits if a["action"] == "路由裁决")
    assert route_audit["detail"]["dest_code"] == "110"
    assert route_audit["detail"]["chute_code"] == "A01"
    assert route_audit["detail"]["line_name"] == "一号皮带"

    barcode_audits = client.get(f"/api/审计?条码={routed_scan['barcode']}").json()
    assert barcode_audits
    assert all(
        (a.get("detail") or {}).get("scan_id", sid) == sid
        or a.get("ref_id") == sid
        for a in barcode_audits
    )

    scans_by_barcode = client.get(
        "/api/扫描册", params={"barcode": routed_scan["barcode"]}
    ).json()
    assert [row["id"] for row in scans_by_barcode] == [sid]

    receipts_by_barcode = client.get(
        "/api/回执", params={"条码": routed_scan["barcode"]}
    ).json()
    assert receipts_by_barcode
    assert all(row["scan_id"] == sid for row in receipts_by_barcode)
    assert {"入袋", "回流"} <= {row["kind"] for row in receipts_by_barcode}

    chutes = client.get("/api/口门").json()
    chute = next(c for c in chutes if c["code"] == "A01")
    assert chute["在口件数"] == 0
    assert chute["回流件数"] == 1


def test_overview_has_no_side_channel_claim_after_divert(client, routed_scan):
    sid = routed_scan["id"]
    body = client.post("/api/回流批次", json={"扫描编号": sid}).json()
    assert body["已回流"] is True
    assert body["已入袋"] is False

    overview = client.get("/api/总览").json()
    assert "surface_ok" not in overview
    assert not any(k.startswith("surface_") for k in overview)
    assert overview["分布"]["错分回流"] == 1
    assert overview["分布"]["已入袋"] == 0
    assert overview["对账"]["平衡"] is True
    assert overview["双挂扫描数"] == 0
    assert overview["冻结宣称"] is False

    settings = client.get("/api/设置").json()
    assert not any(row["键"] in {"surface_ok", "surface_last"} for row in settings)


def test_audit_entity_aliases_and_scan_book_status(client, routed_scan):
    sid = routed_scan["id"]
    chute_id = routed_scan["chute_id"]
    assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200

    query_sets = (
        {"destination": "110"},
        {"destination_code": "110"},
        {"dest": "110"},
        {"chute_code": "A01"},
        {"chuteCode": "A01"},
        {"chute": "A01"},
        {"口门码": "A01"},
        {"chute_id": str(chute_id)},
        {"口门编号": str(chute_id)},
        {"line_name": "一号皮带"},
        {"lineName": "一号皮带"},
        {"line": "一号皮带"},
        {"分拣线": "一号皮带"},
        {"line_id": str(routed_scan["line_id"])},
    )
    for params in query_sets:
        rows = client.get("/api/审计/查询", params=params).json()
        assert any(
            row.get("action") == "路由裁决"
            and (row.get("detail") or {}).get("scan_id") == sid
            for row in rows
        ), (params, rows)

    book = client.get("/api/扫描册", params={"status": "diverted"}).json()
    assert any(row["id"] == sid for row in book)
    assert all(row["status"] == "错分回流" for row in book)

    overview = client.get("/api/总览").json()
    chute = next(c for c in overview["口门"] if c["code"] == "A01")
    assert chute["回流件数"] >= 1


def test_audit_scan_filter_has_no_decimal_prefix_bleed(client):
    # Seed scan id=1.  Create enough lifecycle rows for scan ids >= 10.
    assert client.post("/api/扫描/1/路由").status_code == 200
    for i in range(12):
        barcode = f"YB-AUDIT-BLEED-{i}"
        created = client.post("/api/扫描", json={"条码": barcode, "目的地码": "110"})
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        assert client.post(f"/api/扫描/{sid}/路由").status_code == 200
        if i % 3 == 0:
            assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200

    rows = client.get("/api/审计?扫描编号=1&limit=1000").json()
    assert rows
    for row in rows:
        detail = row.get("detail") or {}
        detail_scan = detail.get("scan_id")
        assert detail_scan in (None, 1)
        if row["kind"] == "扫描":
            assert row["ref_id"] == 1
        else:
            assert detail_scan == 1 or "扫描1" in row.get("note", "")


def test_replay_detects_resurrected_bag_pointer_after_divert(
    client, routed_scan, db_path
):
    sid = routed_scan["id"]
    divert = client.post("/api/回流批次", json={"扫描编号": sid})
    assert divert.status_code == 200

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scans SET bag_id=? WHERE id=?", (99901, sid))
        conn.commit()

    replay = client.get(f"/api/审计/重放?扫描编号={sid}").json()
    assert replay["一致"] is False
    assert any(d.get("field") == "bag_id" for d in replay["差异"])


def test_reconcile_detects_orphan_divert_item_and_dangling_receipt(
    client, routed_scan, db_path
):
    sid = routed_scan["id"]
    assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scans SET status='已路由', divert_batch_id=NULL WHERE id=?",
            (sid,),
        )
        conn.commit()

    rec = client.get("/api/总览/对账").json()
    assert rec["平衡"] is False
    codes = {i["代码"] for i in rec["差异"]}
    assert "DIVERT_ITEM_ORPHAN" in codes
    assert "DIVERT_RECEIPT_DANGLING" in codes

    replay = client.get(f"/api/审计/重放?扫描编号={sid}").json()
    assert replay["一致"] is False


def test_dual_pointer_corruption_freezes_claims(client, routed_scan, open_bag, db_path):
    sid = routed_scan["id"]
    bid = open_bag["id"]
    assert client.post(f"/api/封袋/{bid}/入袋", json={"扫描编号": sid}).status_code == 200
    assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scans SET bag_id=? WHERE id=?", (bid, sid))
        conn.commit()

    scan = client.get(f"/api/扫描/{sid}").json()
    assert scan["dual_attached"] is True
    assert scan["claims_frozen"] is True
    assert scan["已回流"] is False
    assert scan["已入袋"] is False

    overview = client.get("/api/总览").json()
    assert overview["双挂扫描数"] == 1
    assert overview["冻结宣称"] is True
    assert overview["对账"]["平衡"] is False
    assert "BAGGED_WITH_DIVERT_BATCH" in {
        i["代码"] for i in overview["对账"]["差异"]
    } or "DIVERTED_STILL_IN_BAG" in {i["代码"] for i in overview["对账"]["差异"]}


def test_http_divert_half_failure_rolls_back(client, routed_scan, db_path):
    sid = routed_scan["id"]
    with TestClient(app, raise_server_exceptions=False) as fault_client:
        resp = fault_client.post(
            "/api/回流批次",
            json={"扫描编号": sid, "故障点": "divert_after_scan_update"},
        )
    assert resp.status_code == 500

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, version, divert_batch_id FROM scans WHERE id=?",
            (sid,),
        ).fetchone()
        assert row == ("已路由", 2, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM divert_items WHERE scan_id=?", (sid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_receipts WHERE scan_id=?", (sid,)
        ).fetchone()[0] == 0


def test_chinese_surface_aliases_share_same_outcome(client):
    created = client.post("/api/装载", json={"条码": "YB-ALIAS-SURFACE", "目的地码": "110"})
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    assert client.post(f"/api/扫描/{sid}/落入").status_code == 200

    chute = next(c for c in client.get("/api/口门在口").json() if c["code"] == "A01")
    bag = client.post("/api/集包/开袋", json={"口门编号": chute["id"]}).json()
    assert client.post(
        f"/api/集包/{bag['id']}/入袋", json={"scanId": sid}
    ).status_code == 200
    bag_detail = client.get(f"/api/袋册/{bag['id']}").json()
    assert len(bag_detail["members"]) == 1
    assert bag_detail["袋册成员"][0]["id"] == sid
    divert = client.post("/api/回流", json={"scanId": sid})
    assert divert.status_code == 200, divert.text
    batch_id = divert.json()["批次"]["id"]
    assert client.get(f"/api/扫描终态/{sid}").status_code == 200
    assert client.get(f"/api/扫描册/{sid}").status_code == 200
    assert client.get(f"/api/回流批次册/{batch_id}").status_code == 200

    for path in (
        "/api/袋册",
        "/api/回流批次册",
        "/api/扫描终态",
        "/api/班次列",
        "/api/总览/刷新",
        "/api/审计/查询",
    ):
        assert client.get(path).status_code == 200, path

    rec = client.get("/api/总览/刷新").json()["对账"]
    assert rec["平衡"] is True, rec["差异"]


def test_concurrent_half_divert_and_bag_settle_single(
    client, routed_scan, open_bag
):
    sid = routed_scan["id"]
    bid = open_bag["id"]
    barrier = threading.Barrier(2)
    outcomes = {}

    def half_divert():
        barrier.wait()
        try:
            create_divert(sid, fault="divert_after_scan_update")
            outcomes["divert"] = "ok"
        except RuntimeError as exc:
            outcomes["divert"] = f"half:{exc}"
        except HTTPException as exc:
            outcomes["divert"] = f"rejected:{exc.status_code}"
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            outcomes["divert"] = f"fatal:{type(exc).__name__}:{exc}"

    def bag():
        barrier.wait()
        try:
            add_to_bag(bid, sid)
            outcomes["bag"] = "ok"
        except HTTPException as exc:
            outcomes["bag"] = f"rejected:{exc.status_code}"
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            outcomes["bag"] = f"fatal:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        divert_future = pool.submit(half_divert)
        bag_future = pool.submit(bag)
        divert_future.result(timeout=30)
        bag_future.result(timeout=30)

    assert not outcomes["bag"].startswith("fatal"), outcomes
    assert outcomes["bag"] in {"ok", "rejected:400"}, outcomes
    with db(write=False) as conn:
        scan = conn.execute(
            "SELECT status, bag_id, divert_batch_id FROM scans WHERE id=?",
            (sid,),
        ).fetchone()
        if outcomes["bag"] == "ok" and outcomes["divert"].startswith("half:"):
            assert scan["status"] == "已入袋"
            assert scan["bag_id"] == bid
            assert scan["divert_batch_id"] is None
        else:
            assert scan["status"] == "错分回流"
            assert scan["bag_id"] is None

    rec = client.get("/api/总览/对账").json()
    assert rec["平衡"] is True, rec["差异"]
    replay = client.get(f"/api/审计/重放?扫描编号={sid}").json()
    assert replay["一致"] is True, replay["差异"]
