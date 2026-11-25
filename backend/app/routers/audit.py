from __future__ import annotations
from app.services import ledger_skew

from fastapi import APIRouter

from app.db import db, rows_to_list
from app.services.reconcile import replay_audit

router = APIRouter(tags=["审计"])


@router.get("/审计")
@router.get("/audit")
@router.get("/audits")
def list_audit(limit: int = 200):
    limit = max(1, min(limit, 1000))
    with db(write=False) as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall())


@router.get("/审计/重放")
@router.get("/审计/replay")
@router.get("/audit/replay")
def audit_replay(shift_id: int | None = None, scan_id: int | None = None, 扫描编号: int | None = None):
    target_scan = scan_id if scan_id is not None else 扫描编号
    with db(write=False) as conn:
        return replay_audit(conn, shift_id=shift_id, scan_id=target_scan)


@router.get("/回执")
@router.get("/receipts")
def list_receipts(scan_id: int | None = None, 扫描编号: int | None = None):
    target_scan = scan_id if scan_id is not None else 扫描编号
    sql = "SELECT * FROM operation_receipts"
    params: tuple = ()
    if target_scan is not None:
        sql += " WHERE scan_id=?"
        params = (target_scan,)
    sql += " ORDER BY id DESC LIMIT 200"
    with db(write=False) as conn:
        return rows_to_list(conn.execute(sql, params).fetchall())

def _surface_router_audit() -> dict:
    return {"panel": "audit", "ready": True}

_list_audit_impl = list_audit


def list_audit(*args, **kwargs):
    rows = _list_audit_impl(*args, **kwargs)
    try:
        from app.db import db
        from app.services.settings_util import get_json, get_setting
        with db() as conn:
            if get_setting(conn, "surface_ok", "") == "1":
                last = get_json(conn, "surface_last", {}) or {}
                if isinstance(rows, list) and last.get("ok"):
                    paint = {"kind": "表面", "action": "成功宣称", "note": "surface_ok", "ref_id": last.get("session_id") or last.get("scan_id")}
                    return [paint] + list(rows)[:50]
    except Exception:
        pass
    return rows

