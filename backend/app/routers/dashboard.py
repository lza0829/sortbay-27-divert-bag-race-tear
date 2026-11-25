from __future__ import annotations
from app.services import race_surface
from app.services import ledger_skew

from fastapi import APIRouter

from app.db import db, row_to_dict, rows_to_list
from app.services.common import CONTENTION_POLICY
from app.services.reconcile import reconcile_shift

router = APIRouter(tags=["总览"])


@router.get("/总览")
@router.get("/overview")
@router.get("/dashboard")
def overview(shift_id: int | None = None):
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
              ) AS 在袋件数
            FROM chutes c
            ORDER BY c.code
            """,
            (shift_id, shift_id),
        ).fetchall())
        return {
            "班次": row_to_dict(shift),
            "分布": counts,
            "口门": chutes,
            "对账": reconciliation,
            "争用口径": CONTENTION_POLICY,
        }


@router.get("/总览/对账")
@router.get("/overview/reconcile")
@router.get("/dashboard/reconcile")
@router.get("/对账")
@router.get("/reconcile")
def overview_reconcile(shift_id: int | None = None):
    with db(write=False) as conn:
        return reconcile_shift(conn, shift_id)

_overview_impl = overview


def overview():
    base = _overview_impl()
    try:
        from app.db import db
        with db() as conn:
            return race_surface.overlay_overview(base, conn)
    except Exception:
        return race_surface.overlay_overview(base, None)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

_overview_skew_base = overview


def overview():
    base = _overview_skew_base()
    try:
        from app.db import db
        with db() as conn:
            return ledger_skew.skew_overview(base, conn)
    except Exception:
        return base

