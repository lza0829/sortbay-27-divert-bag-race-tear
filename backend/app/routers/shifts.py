from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from app.db import db, row_to_dict, rows_to_list
from app.services.common import audit, now_str
from app.services.reconcile import reconcile_shift

router = APIRouter(tags=["班次"])


class ShiftIn(BaseModel):
    营业日: str = Field(
        min_length=8,
        validation_alias=AliasChoices("营业日", "business_day", "businessDay"),
    )
    班次名: str = Field(
        min_length=1,
        validation_alias=AliasChoices("班次名", "name", "shift_name"),
    )


@router.get("/班次")
@router.get("/班次列")
def list_shifts():
    with db(write=False) as conn:
        return rows_to_list(conn.execute("SELECT * FROM shifts ORDER BY id DESC").fetchall())


@router.get("/班次/对账")
@router.get("/shifts/reconcile")
def reconcile(shift_id: int | None = None, 班次编号: int | None = None):
    shift_id = shift_id if shift_id is not None else 班次编号
    with db(write=False) as conn:
        return reconcile_shift(conn, shift_id)


@router.post("/班次")
def create_shift(body: ShiftIn):
    with db() as conn:
        open_one = conn.execute("SELECT id FROM shifts WHERE sealed=0").fetchone()
        if open_one:
            raise HTTPException(400, "仍有未封班班次，先封班再开新班")
        try:
            cur = conn.execute(
                "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
                (body.营业日, body.班次名),
            )
        except Exception as e:
            raise HTTPException(400, f"开班失败: {e}")
        sid = cur.lastrowid
        row = conn.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
        audit(conn, "班次", row["id"], "开班", body.班次名, shift_id=sid)
        return row_to_dict(row)


@router.post("/班次/{shift_id}/封班")
def seal_shift(shift_id: int):
    with db() as conn:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        if shift["sealed"]:
            raise HTTPException(400, "已封班")
        pending = conn.execute(
            """
            SELECT COUNT(*) c FROM scans
            WHERE shift_id=? AND status != '已封袋'
            """,
            (shift_id,),
        ).fetchone()["c"]
        if pending:
            raise HTTPException(400, f"仍有未封袋/未路由件 {pending}，禁止封班")
        open_bags = conn.execute(
            "SELECT COUNT(*) c FROM bags WHERE shift_id=? AND status='集包中' AND (SELECT COUNT(*) FROM scans s WHERE s.bag_id=bags.id) > 0",
            (shift_id,),
        ).fetchone()["c"]
        if open_bags:
            raise HTTPException(400, f"仍有 {open_bags} 个有件袋未封口，禁止封班")
        conn.execute(
            "UPDATE shifts SET sealed=1, sealed_at=? WHERE id=?",
            (now_str(), shift_id),
        )
        audit(conn, "班次", shift_id, "封班", "", shift_id=shift_id)
        return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone())

def _surface_router_shifts() -> dict:
    return {"panel": "shifts", "ready": True}

