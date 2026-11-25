from __future__ import annotations

from fastapi import APIRouter

from app.db import db, row_to_dict, rows_to_list
from app.services.common import CONTENTION_POLICY
from app.services.reconcile import reconcile_shift

router = APIRouter(tags=["总览"])


@router.get("/总览")
@router.get("/总览/刷新")
@router.get("/overview")
@router.get("/overview/refresh")
@router.get("/dashboard")
def overview(shift_id: int | None = None, 班次编号: int | None = None):
    shift_id = shift_id if shift_id is not None else 班次编号
    with db(write=False) as conn:
        if shift_id is None:
            shift = conn.execute(
                "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not shift:
                empty_distribution = {
                    status: 0 for status in (
                        "待路由", "已路由", "错分回流", "已入袋", "已封袋"
                    )
                }
                empty_distribution.update({
                    "扫描总数": 0,
                    "已路由及以上": 0,
                    "未清件数": 0,
                    "可入袋件数": 0,
                })
                return {
                    "班次": None,
                    "分布": empty_distribution,
                    "口门": [],
                    "对账": {
                        "平衡": True,
                        "balanced": True,
                        "ok": True,
                        "is_balanced": True,
                        "差异": [],
                        "issues": [],
                        "differences": [],
                        "inconsistencies": [],
                        "问题": [],
                        **empty_distribution,
                    },
                    "争用口径": CONTENTION_POLICY,
                    "终态灯": {"回流": 0, "集包": 0, "双挂": 0},
                    "terminal_lights": {"回流": 0, "集包": 0, "双挂": 0},
                    "成功灯": {"回流": 0, "集包": 0, "双挂": 0},
                    "lights": {"divert": 0, "bag": 0, "dual": 0},
                    "平衡": True,
                    "balanced": True,
                    "ok": True,
                    "is_balanced": True,
                    "双挂扫描数": 0,
                    "dual_attached_scans": 0,
                    "dual_success": False,
                    "双成功": False,
                    "冻结宣称": False,
                    "claims_frozen": False,
                    "单一结局": True,
                    "single_outcome": True,
                }
            shift_id = shift["id"]
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()

        reconciliation = reconcile_shift(conn, shift_id)
        counts = {k: reconciliation[k] for k in (
            "待路由", "已路由", "错分回流", "已入袋", "已封袋"
        )}
        chutes = rows_to_list(conn.execute(
            """
            SELECT c.*,
              (
                SELECT COUNT(*) FROM scans s
                WHERE s.chute_id=c.id
                  AND s.shift_id=?
                  AND s.status IN ('已路由','已入袋')
              ) AS 在口件数,
              (
                SELECT COUNT(*) FROM scans s
                WHERE s.chute_id=c.id
                  AND s.shift_id=?
                  AND s.status='已入袋'
              ) AS 在袋件数,
              (
                SELECT COUNT(*) FROM scans s
                WHERE s.chute_id=c.id
                  AND s.shift_id=?
                  AND s.status='错分回流'
              ) AS 回流件数
            FROM chutes c
            ORDER BY c.code
            """,
            (shift_id, shift_id, shift_id),
        ).fetchall())
        return {
            "班次": row_to_dict(shift),
            "分布": counts,
            "终态灯": reconciliation["终态灯"],
            "terminal_lights": reconciliation["terminal_lights"],
            "成功灯": reconciliation["成功灯"],
            "lights": reconciliation["lights"],
            "平衡": reconciliation["平衡"],
            "balanced": reconciliation["balanced"],
            "ok": reconciliation["ok"],
            "is_balanced": reconciliation["is_balanced"],
            "双挂扫描数": reconciliation["双挂扫描数"],
            "dual_attached_scans": reconciliation["dual_attached_scans"],
            "dual_success": False,
            "双成功": False,
            "冻结宣称": reconciliation["冻结宣称"],
            "claims_frozen": reconciliation["claims_frozen"],
            "争用缺口": reconciliation["争用缺口"],
            "contention_gaps": reconciliation["contention_gaps"],
            "单一结局": reconciliation["单一结局"],
            "single_outcome": reconciliation["single_outcome"],
            "口门": chutes,
            "对账": reconciliation,
            "争用口径": CONTENTION_POLICY,
        }


@router.get("/总览/对账")
@router.get("/overview/reconcile")
@router.get("/dashboard/reconcile")
@router.get("/对账")
@router.get("/reconcile")
def overview_reconcile(shift_id: int | None = None, 班次编号: int | None = None):
    shift_id = shift_id if shift_id is not None else 班次编号
    with db(write=False) as conn:
        return reconcile_shift(conn, shift_id)

