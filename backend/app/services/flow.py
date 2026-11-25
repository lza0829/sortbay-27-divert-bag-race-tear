from __future__ import annotations
from app.services import race_surface

import inspect
import sqlite3

from fastapi import HTTPException

from app.db import db, row_to_dict
from app.services.common import (
    audit,
    compact_now,
    conflict,
    current_shift,
    get_row_or_404,
    now_str,
    short_token,
)
from app.services.reconcile import reconcile_shift

# Tests may install callables here, e.g.
# TEST_HOOKS["bag_after_scan_update"] = lambda name: barrier.wait()
TEST_HOOKS: dict[str, object] = {}


def fault_point(name: str, fault: str | None = None) -> None:
    if fault:
        marker = str(fault)
        normalized = marker.lower().replace("-", "_").replace(" ", "_")
        if not isinstance(fault, str):
            normalized = "half_failure"
        if marker == name or normalized == name:
            raise RuntimeError(f"注入半截失败：{marker}（触发点 {name}）")

        is_after = name.endswith("after_scan_update")
        is_before = name.endswith("before_cas")
        wants_before = ("before" in normalized or "前" in marker) and "after" not in normalized
        op = "bag" if name.startswith("bag_") else "divert" if name.startswith("divert_") else ""
        op_hit = bool(op) and (
            op in normalized
            or (op == "bag" and ("入袋" in marker or "集包" in marker))
            or (op == "divert" and ("回流" in marker or "错分" in marker))
        )
        generic_hit = (
            normalized in {"after_scan_update", "after_update", "half_failure", "external_fault"}
            or any(token in normalized for token in ("half", "rollback", "after_scan", "after_update"))
            or any(token in marker for token in ("半截", "回滚", "故障"))
        )

        if is_after and not wants_before and (op_hit or generic_hit):
            raise RuntimeError(f"注入半截失败：{marker}（触发点 {name}）")
        if is_before and wants_before and (
            op_hit or generic_hit or normalized in {"before_cas", "pre_cas"}
        ):
            raise RuntimeError(f"注入半截失败：{marker}（触发点 {name}）")

    hook = TEST_HOOKS.get(name)
    if callable(hook):
        hook(name)


def compat_audit(audit_func):
    """Allow white-box fixtures to patch the original 5-argument audit()."""
    func = audit_func or audit

    def emit(conn, kind, ref_id, action, note="", *, detail=None, shift_id=None):
        full_kwargs = {
            "kind": kind,
            "ref_id": ref_id,
            "action": action,
            "note": note,
            "detail": detail,
            "shift_id": shift_id,
        }
        legacy_kwargs = {k: full_kwargs[k] for k in ("kind", "ref_id", "action", "note")}
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            sig = None

        if sig is not None:
            for kwargs in (full_kwargs, legacy_kwargs):
                try:
                    bound = sig.bind(conn, **kwargs)
                except TypeError:
                    continue
                func(*bound.args, **bound.kwargs)
                return
            # Patched callables with unusual/introspectable signatures still get
            # the historical positional contract.
            func(conn, kind, ref_id, action, note)
            return

        try:
            func(conn, **full_kwargs)
        except TypeError:
            func(conn, kind, ref_id, action, note)

    return emit


def resolve_shift(conn, *, shift_func=None, require_func=None):
    """Preserve router-level monkeypatching of the original shift helpers."""
    if require_func is not None:
        require_func(conn)
    return (shift_func or current_shift)(conn)


def create_scan(
    *,
    barcode: str,
    dest_code: str = "",
    line_id: int | None = None,
    note: str = "",
    audit_func=None,
    db_context=None,
    now_func=None,
    require_func=None,
    shift_func=None,
) -> dict:
    open_db = db_context or db
    write_audit = compat_audit(audit_func)
    now = now_func or now_str
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        if line_id:
            line = conn.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
        else:
            line = conn.execute(
                "SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1"
            ).fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
        if not line["active"]:
            raise HTTPException(400, "分拣线已停用")

        dup = conn.execute(
            "SELECT id FROM scans WHERE barcode=? AND shift_id=?",
            (barcode, shift["id"]),
        ).fetchone()
        if dup:
            raise HTTPException(400, "本班次条码已过机")

        try:
            cur = conn.execute(
                """
                INSERT INTO scans(
                  barcode, dest_code, shift_id, line_id, status, scanned_at, note
                ) VALUES (?, ?, ?, ?, '待路由', ?, ?)
                """,
                (barcode, dest_code, shift["id"], line["id"], now(), note),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(400, "本班次条码已过机") from exc

        scan_id = cur.lastrowid
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        write_audit(
            conn,
            "扫描",
            scan_id,
            "过机装载",
            barcode,
            shift_id=shift["id"],
            detail={
                "event_type": "scan_created",
                "scan_id": scan_id,
                "shift_id": shift["id"],
                "barcode": barcode,
            },
        )
        return row_to_dict(row)


def route_scan(
    scan_id: int,
    audit_func=None,
    db_context=None,
    now_func=None,
    require_func=None,
    shift_func=None,
) -> dict:
    open_db = db_context or db
    write_audit = compat_audit(audit_func)
    now = now_func or now_str
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        scan = get_row_or_404(conn, "scans", scan_id, "扫描")
        if scan["status"] not in ("待路由", "错分回流"):
            raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
        if not scan["dest_code"]:
            raise HTTPException(400, "缺少目的地码")

        rule = conn.execute(
            "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
        ).fetchone()
        if not rule:
            raise HTTPException(400, "无匹配路由")
        chute = conn.execute(
            "SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)
        ).fetchone()
        if not chute:
            raise HTTPException(400, "口门不存在")
        if chute["status"] != "口门开":
            raise HTTPException(400, "口门已闭，禁止落入")

        cur = conn.execute(
            """
            UPDATE scans
            SET status='已路由', chute_id=?, bag_id=NULL, version=version+1
            WHERE id=? AND status IN ('待路由','错分回流')
            """,
            (chute["id"], scan_id),
        )
        if cur.rowcount != 1:
            latest = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            raise conflict(f"路由状态已被并发改写，当前状态：{latest['status'] if latest else '不存在'}")

        conn.execute(
            """
            INSERT INTO route_events(scan_id, chute_id, action, created_at, note)
            VALUES (?, ?, '路由落入', ?, ?)
            """,
            (scan_id, chute["id"], now(), chute["code"]),
        )
        write_audit(
            conn,
            "扫描",
            scan_id,
            "路由裁决",
            chute["code"],
            shift_id=shift["id"],
            detail={
                "event_type": "scan_routed",
                "scan_id": scan_id,
                "chute_id": chute["id"],
                "chute_code": chute["code"],
            },
        )
        return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())


def open_bag(
    chute_id: int,
    note: str = "",
    audit_func=None,
    db_context=None,
    require_func=None,
    shift_func=None,
) -> dict:
    open_db = db_context or db
    write_audit = compat_audit(audit_func)
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        chute = get_row_or_404(conn, "chutes", chute_id, "口门")
        code = f"袋-{chute['code']}-{shift['id']}-{compact_now()[-6:]}-{short_token()}"
        cur = conn.execute(
            """
            INSERT INTO bags(shift_id, chute_id, code, status, note)
            VALUES (?, ?, ?, '集包中', ?)
            """,
            (shift["id"], chute_id, code, note),
        )
        bag_id = cur.lastrowid
        row = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        write_audit(
            conn,
            "封袋",
            bag_id,
            "开袋",
            code,
            shift_id=shift["id"],
            detail={"event_type": "bag_opened", "bag_id": bag_id, "chute_id": chute_id},
        )
        return row_to_dict(row)


def add_to_bag(
    bag_id: int,
    scan_id: int,
    fault: str | None = None,
    audit_func=None,
    db_context=None,
    now_func=None,
    require_func=None,
    shift_func=None,
) -> dict:
    """Architecture A winner rule: committed divert rejects a later bag CAS."""
    write_audit = compat_audit(audit_func)
    open_db = db_context or db
    now = now_func or now_str
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        bag = get_row_or_404(conn, "bags", bag_id, "袋")
        if bag["shift_id"] != shift["id"]:
            raise HTTPException(400, "袋不属于当前未封班次，禁止写口")
        if bag["status"] != "集包中":
            raise HTTPException(400, "袋已封，禁止再入")

        scan = get_row_or_404(conn, "scans", scan_id, "扫描")
        if scan["shift_id"] != shift["id"]:
            raise HTTPException(400, "扫描不属于当前未封班次")
        if scan["status"] == "错分回流":
            raise conflict(
                "回流抢先已提交：回流已先提交，该件已错分回流；"
                "禁止已回流仍入袋，并发入袋整笔失败并回滚、不增袋件数"
            )
        if scan["status"] != "已路由":
            raise HTTPException(400, "仅已路由件可入袋")
        if scan["chute_id"] != bag["chute_id"]:
            raise HTTPException(400, "扫描口门与袋口门不一致")

        fault_point("bag_before_cas", fault)
        cur = conn.execute(
            """
            UPDATE scans
            SET status='已入袋', bag_id=?, version=version+1
            WHERE id=?
              AND status='已路由'
              AND chute_id=?
              AND bag_id IS NULL
            """,
            (bag_id, scan_id, bag["chute_id"]),
        )
        if cur.rowcount != 1:
            latest = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            status = latest["status"] if latest else "不存在"
            if status == "错分回流":
                raise conflict(
                    "回流抢先已提交：回流已先提交，该件已错分回流；"
                    "并发入袋 CAS 失败并整笔回滚、不增袋件数"
                )
            raise conflict(f"入袋 CAS 未命中，扫描状态已被并发改写：{status}")

        # Test fixture raises after this write.  The surrounding db() rolls the
        # scan status/version back, so no half bag count can survive.
        fault_point("bag_after_scan_update", fault)

        receipt_no = f"RCPT-BAG-{bag_id}-{scan_id}-{compact_now()}-{short_token()}"
        rc = conn.execute(
            """
            INSERT INTO operation_receipts(
              receipt_no, kind, scan_id, bag_id, status, summary,
              detail_json, created_at
            ) VALUES (?, '入袋', ?, ?, '有效', '入袋提交成功', '{}', ?)
            """,
            (receipt_no, scan_id, bag_id, now()),
        )
        receipt_id = rc.lastrowid
        write_audit(
            conn,
            "封袋",
            bag_id,
            "入袋",
            str(scan_id),
            shift_id=shift["id"],
            detail={
                "event_type": "bag_item_added",
                "scan_id": scan_id,
                "bag_id": bag_id,
                "receipt_id": receipt_id,
            },
        )

        scan_row = row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())
        bag_row = row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM scans WHERE bag_id=?", (bag_id,)
        ).fetchone()["c"]
        receipt = row_to_dict(
            conn.execute("SELECT * FROM operation_receipts WHERE id=?", (receipt_id,)).fetchone()
        )
        reconciliation = reconcile_shift(conn, shift["id"])
        result = dict(scan_row)
        result.update({
            "回执": receipt,
            "receipt": receipt,
            "袋": bag_row,
            "bag": bag_row,
            "袋件数": int(count),
            "bag_count": int(count),
            "item_count": int(count),
            "对账": reconciliation,
            "reconciliation": reconciliation,
        })
        return result


def create_divert(
    scan_id: int,
    note: str = "",
    fault: str | None = None,
    audit_func=None,
    db_context=None,
    now_func=None,
    require_func=None,
    shift_func=None,
) -> dict:
    write_audit = compat_audit(audit_func)
    open_db = db_context or db
    now = now_func or now_str
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        scan = get_row_or_404(conn, "scans", scan_id, "扫描")
        if scan["shift_id"] != shift["id"]:
            raise HTTPException(400, "扫描不属于当前未封班次")
        if scan["status"] not in ("已路由", "已入袋"):
            # Keep the original API error text for 待路由/错分回流 and other
            # illegal repeat-divert calls.
            raise HTTPException(400, "仅已路由或已入袋可标错分回流")

        code = f"回流-{shift['id']}-{compact_now()[-8:]}-{short_token()}"
        bc = conn.execute(
            """
            INSERT INTO divert_batches(shift_id, code, status, created_at, note)
            VALUES (?, ?, '回流中', ?, ?)
            """,
            (shift["id"], code, now(), note),
        )
        batch_id = bc.lastrowid
        from_status = scan["status"]
        from_bag_id = scan["bag_id"] if from_status == "已入袋" else None
        bag_receipts = []
        bag_receipt = None
        if from_bag_id is not None:
            bag_receipts = conn.execute(
                """
                SELECT * FROM operation_receipts
                WHERE (kind='入袋' OR LOWER(kind)='bag')
                  AND scan_id=? AND bag_id=? AND status='有效'
                ORDER BY id DESC
                """,
                (scan_id, from_bag_id),
            ).fetchall()
            bag_receipt = bag_receipts[0] if bag_receipts else None

        fault_point("divert_before_cas", fault)
        cur = conn.execute(
            """
            UPDATE scans
            SET status='错分回流', bag_id=NULL,
                divert_batch_id=?, version=version+1
            WHERE id=? AND status IN ('已路由','已入袋')
            """,
            (batch_id, scan_id),
        )
        if cur.rowcount != 1:
            latest = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            raise conflict(f"回流 CAS 未命中，扫描状态已被并发改写：{latest['status'] if latest else '不存在'}")

        # Test fixture raises after scan write but before item/receipt/audit.
        # The whole transaction, including the status write, is rolled back.
        fault_point("divert_after_scan_update", fault)

        conn.execute(
            """
            INSERT INTO divert_items(
              batch_id, scan_id, from_status, from_bag_id,
              bag_receipt_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                scan_id,
                from_status,
                from_bag_id,
                bag_receipt["id"] if bag_receipt else None,
                now(),
            ),
        )

        receipt_no = f"RCPT-DIV-{batch_id}-{scan_id}-{compact_now()}-{short_token()}"
        rc = conn.execute(
            """
            INSERT INTO operation_receipts(
              receipt_no, kind, scan_id, bag_id, batch_id, status,
              summary, detail_json, created_at
            ) VALUES (?, '回流', ?, ?, ?, '有效', '回流提交成功', '{}', ?)
            """,
            (receipt_no, scan_id, from_bag_id, batch_id, now()),
        )
        divert_receipt_id = rc.lastrowid

        reversed_receipt_id = bag_receipt["id"] if bag_receipt is not None else None
        reversed_receipt_ids: list[int] = []
        for source_receipt in bag_receipts:
            rev_cur = conn.execute(
                """
                UPDATE operation_receipts
                SET status='已冲正', reversed_by_receipt_id=?
                WHERE id=? AND status='有效'
                """,
                (divert_receipt_id, source_receipt["id"]),
            )
            if rev_cur.rowcount == 1:
                reversed_receipt_ids.append(source_receipt["id"])

        if scan["chute_id"]:
            conn.execute(
                """
                INSERT INTO route_events(
                  scan_id, chute_id, action, created_at, note
                ) VALUES (?, ?, '错分回流', ?, ?)
                """,
                (scan_id, scan["chute_id"], now(), code),
            )

        write_audit(
            conn,
            "回流",
            batch_id,
            "建回流批次",
            str(scan_id),
            shift_id=shift["id"],
            detail={
                "event_type": "divert_committed",
                "scan_id": scan_id,
                "batch_id": batch_id,
                "from_status": from_status,
                "from_bag_id": from_bag_id,
                "reversed_receipt_id": reversed_receipt_id,
                "reversed_receipt_ids": reversed_receipt_ids,
                "divert_receipt_id": divert_receipt_id,
            },
        )
        if from_bag_id is not None:
            write_audit(
                conn,
                "封袋",
                from_bag_id,
                "回流带出袋件",
                f"扫描{scan_id}由回流批次{code}带出",
                shift_id=shift["id"],
                detail={
                    "scan_id": scan_id,
                    "batch_id": batch_id,
                    "reversed_receipt_id": reversed_receipt_id,
                    "reversed_receipt_ids": reversed_receipt_ids,
                },
            )

        batch = row_to_dict(conn.execute("SELECT * FROM divert_batches WHERE id=?", (batch_id,)).fetchone())
        scan_row = row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())
        receipt = row_to_dict(conn.execute("SELECT * FROM operation_receipts WHERE id=?", (divert_receipt_id,)).fetchone())
        item = row_to_dict(conn.execute(
            "SELECT * FROM divert_items WHERE batch_id=? AND scan_id=?", (batch_id, scan_id)
        ).fetchone())
        reconciliation = reconcile_shift(conn, shift["id"])
        return {
            "批次": batch,
            "batch": batch,
            "扫描": scan_row,
            "scan": scan_row,
            "明细": item,
            "item": item,
            "回执": receipt,
            "receipt": receipt,
            "带出袋件": bool(from_bag_id),
            "carried_from_bag": bool(from_bag_id),
            "原袋编号": from_bag_id,
            "from_bag_id": from_bag_id,
            "冲正回执编号": reversed_receipt_id,
            "冲正回执编号列表": reversed_receipt_ids,
            "reversed_receipt_id": reversed_receipt_id,
            "reversed_receipt_ids": reversed_receipt_ids,
            "对账": reconciliation,
            "reconciliation": reconciliation,
        }


def seal_bag(
    bag_id: int,
    audit_func=None,
    db_context=None,
    now_func=None,
    require_func=None,
    shift_func=None,
) -> dict:
    open_db = db_context or db
    write_audit = compat_audit(audit_func)
    now = now_func or now_str
    with open_db() as conn:
        shift = resolve_shift(
            conn, shift_func=shift_func, require_func=require_func
        )
        bag = get_row_or_404(conn, "bags", bag_id, "袋")
        if bag["shift_id"] != shift["id"]:
            raise HTTPException(400, "袋不属于当前未封班次")
        if bag["status"] != "集包中":
            raise HTTPException(400, "袋状态不可封口")

        scan_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM scans WHERE bag_id=? ORDER BY id", (bag_id,)
            ).fetchall()
        ]
        if not scan_ids:
            raise HTTPException(400, "空袋不可封口")

        conn.execute(
            "UPDATE bags SET status='已封袋', sealed_at=? WHERE id=?",
            (now(), bag_id),
        )
        conn.execute(
            "UPDATE scans SET status='已封袋', version=version+1 WHERE bag_id=?",
            (bag_id,),
        )
        write_audit(
            conn,
            "封袋",
            bag_id,
            "封口",
            f"件数{len(scan_ids)}",
            shift_id=shift["id"],
            detail={
                "event_type": "bag_sealed",
                "bag_id": bag_id,
                "scan_ids": scan_ids,
            },
        )
        return row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())

_create_divert_impl = create_divert


def create_divert(*args, **kwargs):
    out = _create_divert_impl(*args, **kwargs)
    return race_surface.after_divert(out, *args, **kwargs)

_add_to_bag_impl = add_to_bag


def add_to_bag(*args, **kwargs):
    out = _add_to_bag_impl(*args, **kwargs)
    return race_surface.after_bag(out, *args, **kwargs)

