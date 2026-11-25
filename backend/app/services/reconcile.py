from __future__ import annotations

import json

from fastapi import HTTPException

from app.db import row_to_dict, rows_to_list
from app.services.common import CONTENTION_POLICY, latest_shift

SCAN_STATUSES = ("待路由", "已路由", "错分回流", "已入袋", "已封袋")
VALID_RECEIPT_STATUSES = {
    "有效", "成功", "已提交", "valid", "active", "success", "succeeded", "committed", "ok"
}
REVERSED_RECEIPT_STATUSES = {
    "已冲正", "冲正", "已撤销", "reversed", "reversal", "void", "voided", "cancelled", "canceled"
}


def receipt_is_valid(status: object) -> bool:
    text = str(status or "").strip()
    return text in VALID_RECEIPT_STATUSES or text.lower() in VALID_RECEIPT_STATUSES


def receipt_is_reversed(status: object) -> bool:
    text = str(status or "").strip()
    return text in REVERSED_RECEIPT_STATUSES or text.lower() in REVERSED_RECEIPT_STATUSES


def add_issue(issues: list[dict], code: str, message: str, **extra) -> None:
    item = {
        "代码": code,
        "code": code,
        "type": code,
        "说明": message,
        "message": message,
    }
    item.update(extra)
    issues.append(item)


def shift_counts(conn, shift_id: int) -> dict:
    grouped = {
        r["status"]: r["c"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM scans WHERE shift_id=? GROUP BY status",
            (shift_id,),
        ).fetchall()
    }
    counts = {status: int(grouped.get(status, 0)) for status in SCAN_STATUSES}
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM scans WHERE shift_id=?", (shift_id,)
    ).fetchone()["c"]
    counts["扫描总数"] = int(total)
    counts["已路由及以上"] = counts["已路由"] + counts["已入袋"] + counts["已封袋"]
    # 与封班闸门保持同一口径：只有“已封袋”是终态。
    counts["未清件数"] = total - counts["已封袋"]
    counts["可入袋件数"] = counts["已路由"]
    return counts


def reconcile_shift(conn, shift_id: int | None = None) -> dict:
    if shift_id is None:
        shift = latest_shift(conn)
        shift_id = shift["id"]
    else:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")

    issues: list[dict] = []
    counts = shift_counts(conn, shift_id)

    scans = rows_to_list(conn.execute(
        "SELECT * FROM scans WHERE shift_id=? ORDER BY id", (shift_id,)
    ).fetchall())
    bags = rows_to_list(conn.execute(
        "SELECT * FROM bags WHERE shift_id=? ORDER BY id", (shift_id,)
    ).fetchall())
    bag_ids = {b["id"] for b in bags}
    bag_by_id = {b["id"]: b for b in bags}

    for scan in scans:
        sid = scan["id"]
        status = scan["status"]
        if status not in SCAN_STATUSES:
            add_issue(
                issues,
                "UNKNOWN_SCAN_STATUS",
                "扫描状态机出现未知/半截状态",
                扫描编号=sid,
                当前状态=status,
            )
        if status == "已路由":
            if not scan["chute_id"]:
                add_issue(issues, "ROUTED_WITHOUT_CHUTE", "已路由件缺少口门", 扫描编号=sid)
            if scan["bag_id"] is not None:
                add_issue(issues, "ROUTED_STILL_IN_BAG", "已路由件仍挂袋", 扫描编号=sid, 袋编号=scan["bag_id"])
        elif status == "错分回流":
            if scan["bag_id"] is not None:
                add_issue(
                    issues,
                    "DIVERTED_STILL_IN_BAG",
                    "已回流件仍增/仍挂袋件数",
                    扫描编号=sid,
                    袋编号=scan["bag_id"],
                )
            if not scan["divert_batch_id"]:
                add_issue(issues, "DIVERT_WITHOUT_BATCH", "错分回流件缺少回流批次", 扫描编号=sid)
            else:
                item = conn.execute(
                    "SELECT id FROM divert_items WHERE batch_id=? AND scan_id=?",
                    (scan["divert_batch_id"], sid),
                ).fetchone()
                if not item:
                    add_issue(
                        issues,
                        "DIVERT_BATCH_ITEM_MISSING",
                        "回流批次缺少该件明细",
                        扫描编号=sid,
                        批次编号=scan["divert_batch_id"],
                    )
        elif status in ("已入袋", "已封袋"):
            bid = scan["bag_id"]
            if bid is None:
                add_issue(issues, "BAGGED_WITHOUT_BAG", f"{status}件缺少袋编号", 扫描编号=sid)
            elif bid not in bag_ids:
                add_issue(issues, "BAG_FK_DANGLING", "扫描指向不存在的袋", 扫描编号=sid, 袋编号=bid)
            else:
                bag = bag_by_id[bid]
                if scan["chute_id"] != bag["chute_id"]:
                    add_issue(
                        issues,
                        "CHUTE_BAG_MISMATCH",
                        "扫描口门与袋口门不一致",
                        扫描编号=sid,
                        袋编号=bid,
                    )
                expected_bag_status = "集包中" if status == "已入袋" else "已封袋"
                if bag["status"] != expected_bag_status:
                    add_issue(
                        issues,
                        "SCAN_BAG_STATUS_TWIST",
                        "袋件数状态与袋状态拧花",
                        扫描编号=sid,
                        袋编号=bid,
                        扫描状态=status,
                        袋状态=bag["status"],
                    )
                receipt = conn.execute(
                    """
                    SELECT id FROM operation_receipts
                    WHERE (kind='入袋' OR LOWER(kind)='bag')
                      AND scan_id=? AND bag_id=?
                      AND (
                        status IN ('有效', '成功', '已提交')
                        OR LOWER(status) IN (
                          'valid', 'active', 'success', 'succeeded', 'committed', 'ok'
                        )
                      )
                    ORDER BY id DESC LIMIT 1
                    """,
                    (sid, bid),
                ).fetchone()
                if not receipt:
                    add_issue(
                        issues,
                        "BAG_RECEIPT_MISSING",
                        "件已在袋但缺少有效入袋回执",
                        扫描编号=sid,
                        袋编号=bid,
                    )

    # 有效入袋回执必须能在袋件集合中落地；被回流冲正的回执必须有去向。
    receipts = rows_to_list(conn.execute(
        """
        SELECT r.* FROM operation_receipts r
        LEFT JOIN scans s ON s.id=r.scan_id
        WHERE (r.kind='入袋' OR LOWER(r.kind)='bag')
          AND (s.shift_id=? OR s.id IS NULL)
        ORDER BY r.id
        """,
        (shift_id,),
    ).fetchall())
    for receipt in receipts:
        scan = next((s for s in scans if s["id"] == receipt["scan_id"]), None)
        if receipt_is_valid(receipt["status"]):
            if (
                scan is None
                or scan["bag_id"] != receipt["bag_id"]
                or scan["status"] not in ("已入袋", "已封袋")
            ):
                add_issue(
                    issues,
                    "BAG_RECEIPT_DANGLING",
                    "入袋回执成功却无对应在袋袋件，且未见冲正/回流带出",
                    回执编号=receipt["id"],
                    扫描编号=receipt["scan_id"],
                    袋编号=receipt["bag_id"],
                )
        elif receipt_is_reversed(receipt["status"]) and not receipt["reversed_by_receipt_id"]:
            add_issue(
                issues,
                "REVERSAL_WITHOUT_LINK",
                "入袋回执已冲正但缺少回流回执链接",
                回执编号=receipt["id"],
                扫描编号=receipt["scan_id"],
            )

    bag_rows = rows_to_list(conn.execute(
        """
        SELECT b.*, c.code AS 口门码,
               COUNT(s.id) AS 件数
        FROM bags b
        JOIN chutes c ON c.id=b.chute_id
        LEFT JOIN scans s ON s.bag_id=b.id
        WHERE b.shift_id=?
        GROUP BY b.id
        ORDER BY b.id DESC
        """,
        (shift_id,),
    ).fetchall())
    for bag in bag_rows:
        if bag["status"] == "已封袋" and bag["件数"] == 0:
            add_issue(issues, "SEALED_EMPTY_BAG", "已封袋袋件数为 0", 袋编号=bag["id"])

    chute_rows = rows_to_list(conn.execute(
        """
        SELECT c.id, c.code, c.status,
               COUNT(s.id) AS 在口件数
        FROM chutes c
        LEFT JOIN scans s
          ON s.chute_id=c.id
         AND s.shift_id=?
         AND s.status IN ('已路由','已入袋')
        GROUP BY c.id
        ORDER BY c.code
        """,
        (shift_id,),
    ).fetchall())

    batch_count = conn.execute(
        "SELECT COUNT(*) AS c FROM divert_batches WHERE shift_id=?", (shift_id,)
    ).fetchone()["c"]
    divert_item_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM divert_items di
        JOIN divert_batches db ON db.id=di.batch_id
        WHERE db.shift_id=?
        """,
        (shift_id,),
    ).fetchone()["c"]
    sealed_bag = conn.execute(
        "SELECT COUNT(*) AS c FROM bags WHERE shift_id=? AND status='已封袋'",
        (shift_id,),
    ).fetchone()["c"]

    return {
        "班次": row_to_dict(shift),
        "争用口径": CONTENTION_POLICY,
        **counts,
        "袋件数": counts["已入袋"] + counts["已封袋"],
        "袋数": len(bags),
        "已封袋数": int(sealed_bag),
        "回流批次数": int(batch_count),
        "回流件数": int(divert_item_count),
        "袋对账": bag_rows,
        "口门对账": chute_rows,
        "平衡": not issues,
        "ok": not issues,
        "balanced": not issues,
        "is_balanced": not issues,
        "差异": issues,
        "differences": issues,
        "issues": issues,
        "inconsistencies": issues,
        "问题": [i["说明"] for i in issues],
    }


def replay_audit(conn, shift_id: int | None = None, scan_id: int | None = None) -> dict:
    """Fold structured audit events back into scan/bag state.

    The replay is deliberately limited to state-transition events.  It gives
    contention outcomes a deterministic, auditable second representation.
    """
    sql = "SELECT * FROM audit_events ORDER BY id"
    params: tuple = ()
    if shift_id is not None:
        sql = "SELECT * FROM audit_events WHERE shift_id=? OR shift_id IS NULL ORDER BY id"
        params = (shift_id,)
    events = rows_to_list(conn.execute(sql, params).fetchall())

    scans: dict[int, dict] = {}
    bags: dict[int, dict] = {}
    used = 0

    for event in events:
        try:
            detail = json.loads(event.get("detail_json") or "{}")
        except json.JSONDecodeError:
            continue
        et = detail.get("event_type")
        if not et:
            continue
        used += 1

        if et == "scan_created":
            sid = int(detail["scan_id"])
            scans[sid] = {
                "status": "待路由",
                "chute_id": None,
                "bag_id": None,
                "divert_batch_id": None,
            }
        elif et == "scan_routed":
            sid = int(detail["scan_id"])
            scans.setdefault(sid, {
                "status": None, "chute_id": None, "bag_id": None, "divert_batch_id": None
            })
            scans[sid].update(status="已路由", chute_id=detail.get("chute_id"), bag_id=None)
        elif et == "bag_opened":
            bid = int(detail["bag_id"])
            bags[bid] = {"status": "集包中", "items": set(), "touched": set()}
        elif et == "bag_item_added":
            sid = int(detail["scan_id"])
            bid = int(detail["bag_id"])
            scans.setdefault(sid, {
                "status": None, "chute_id": None, "bag_id": None, "divert_batch_id": None
            })
            scans[sid].update(status="已入袋", bag_id=bid)
            bag_state = bags.setdefault(
                bid, {"status": "集包中", "items": set(), "touched": set()}
            )
            bag_state["items"].add(sid)
            bag_state["touched"].add(sid)
        elif et == "divert_committed":
            sid = int(detail["scan_id"])
            batch_id = detail.get("batch_id")
            from_bag = detail.get("from_bag_id")
            scans.setdefault(sid, {
                "status": None, "chute_id": None, "bag_id": None, "divert_batch_id": None
            })
            scans[sid].update(status="错分回流", bag_id=None, divert_batch_id=batch_id)
            if from_bag is not None:
                bag_state = bags.setdefault(
                    int(from_bag), {"status": "集包中", "items": set(), "touched": set()}
                )
                # Final membership is removed, but touched retains this scan so
                # a scan-specific replay still checks the source bag count.
                bag_state["items"].discard(sid)
                bag_state["touched"].add(sid)
        elif et == "bag_sealed":
            bid = int(detail["bag_id"])
            bag_state = bags.setdefault(
                bid, {"status": "集包中", "items": set(), "touched": set()}
            )
            bag_state["status"] = "已封袋"
            for sid in detail.get("scan_ids", []):
                sid = int(sid)
                scans.setdefault(sid, {
                    "status": None, "chute_id": None, "bag_id": bid, "divert_batch_id": None
                })
                scans[sid].update(status="已封袋", bag_id=bid)
                bag_state["items"].add(sid)
                bag_state["touched"].add(sid)

    differences: list[dict] = []
    target_ids = {scan_id} if scan_id is not None else set(scans)
    for sid in sorted(target_ids):
        expected = scans.get(sid)
        if expected is None:
            continue
        current = conn.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
        if current is None:
            differences.append({
                "扫描编号": sid,
                "代码": "MISSING_SCAN",
                "code": "MISSING_SCAN",
                "type": "MISSING_SCAN",
                "说明": "重放得到扫描但当前库不存在",
                "message": "重放得到扫描但当前库不存在",
            })
            continue
        for key in ("status", "chute_id", "bag_id", "divert_batch_id"):
            if expected.get(key) is not None and current[key] != expected[key]:
                differences.append({
                    "扫描编号": sid,
                    "字段": key,
                    "field": key,
                    "重放值": expected[key],
                    "expected": expected[key],
                    "当前值": current[key],
                    "actual": current[key],
                    "代码": "REPLAY_FIELD_MISMATCH",
                    "code": "REPLAY_FIELD_MISMATCH",
                    "type": "REPLAY_FIELD_MISMATCH",
                    "说明": "审计重放状态与当前落账不一致",
                    "message": "审计重放状态与当前落账不一致",
                })

    for bid, expected_bag in bags.items():
        if scan_id is not None and scan_id not in expected_bag["touched"]:
            continue
        current_count = conn.execute(
            "SELECT COUNT(*) AS c FROM scans WHERE bag_id=?", (bid,)
        ).fetchone()["c"]
        if current_count != len(expected_bag["items"]):
            differences.append({
                "袋编号": bid,
                "代码": "BAG_COUNT_REPLAY_MISMATCH",
                "code": "BAG_COUNT_REPLAY_MISMATCH",
                "type": "BAG_COUNT_REPLAY_MISMATCH",
                "重放件数": len(expected_bag["items"]),
                "expected_count": len(expected_bag["items"]),
                "当前件数": current_count,
                "actual_count": current_count,
                "说明": "审计重放袋件数与当前袋件数不一致",
                "message": "审计重放袋件数与当前袋件数不一致",
            })

    replayed_scan = None
    if scan_id is not None and scan_id in scans:
        replayed_scan = {"扫描编号": scan_id, **scans[scan_id]}

    return {
        "一致": not differences,
        "ok": not differences,
        "consistent": not differences,
        "is_consistent": not differences,
        "已重放事件数": used,
        "replayed_events": used,
        "重放扫描数": len(scans),
        "replayed_scans": len(scans),
        "差异": differences,
        "differences": differences,
        "指定扫描": replayed_scan,
        "target_scan": replayed_scan,
        "口径": CONTENTION_POLICY,
        "policy": CONTENTION_POLICY,
    }
