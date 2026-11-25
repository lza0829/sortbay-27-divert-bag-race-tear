from __future__ import annotations
from app.services import race_surface

from fastapi import APIRouter, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.db import db, row_to_dict, rows_to_list
from app.services.common import (
    audit,
    current_shift,
    extract_fault,
    now_str,
    require_unsealed,
)
from app.services.flow import create_divert as create_divert_service

__legacy_symbols__ = (HTTPException, Field, current_shift, require_unsealed)

router = APIRouter(tags=["错分回流"])


class DivertIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    扫描编号: int = Field(
        validation_alias=AliasChoices("扫描编号", "scan_id", "scanId")
    )
    备注: str = Field(
        default="",
        validation_alias=AliasChoices("备注", "note", "remark"),
    )
    # 在显式故障开关或 pytest 运行中生效，供半截回滚夹具经 HTTP 注入故障。
    故障点: object | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "故障点", "故障", "注入故障", "失败点",
            "fault", "fault_point", "failure_point", "inject_fault",
        ),
    )


@router.get("/回流批次")
@router.get("/diverts")
@router.get("/divert-batches")
def list_batches():
    with db(write=False) as conn:
        return rows_to_list(conn.execute(
            """
            SELECT b.*,
                   COUNT(di.id) AS 件数,
                   COALESCE(SUM(CASE WHEN di.from_bag_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS 带出袋件数
            FROM divert_batches b
            LEFT JOIN divert_items di ON di.batch_id=b.id
            GROUP BY b.id
            ORDER BY b.id DESC LIMIT 100
            """
        ).fetchall())


@router.get("/回流批次/{batch_id}")
@router.get("/diverts/{batch_id}")
@router.get("/divert-batches/{batch_id}")
def get_batch(batch_id: int):
    with db(write=False) as conn:
        batch = conn.execute("SELECT * FROM divert_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, "回流批次不存在")
        items = rows_to_list(conn.execute(
            """
            SELECT di.*, s.barcode, s.status AS 当前扫描状态,
                   b.code AS 原袋码
            FROM divert_items di
            JOIN scans s ON s.id=di.scan_id
            LEFT JOIN bags b ON b.id=di.from_bag_id
            WHERE di.batch_id=?
            ORDER BY di.id
            """,
            (batch_id,),
        ).fetchall())
        result = row_to_dict(batch)
        result["明细"] = items
        result["items"] = items
        result["件数"] = len(items)
        return result


@router.post("/回流批次")
@router.post("/diverts")
@router.post("/divert-batches")
def create_divert(body: DivertIn):
    fault = extract_fault(body)
    return create_divert_service(
        body.扫描编号,
        body.备注,
        fault=fault,
        audit_func=audit,
        db_context=db,
        now_func=now_str,
        require_func=require_unsealed,
        shift_func=current_shift,
    )

_create_divert_impl = create_divert


def create_divert(*args, **kwargs):
    out = _create_divert_impl(*args, **kwargs)
    return race_surface.after_divert(out, *args, **kwargs)

def _surface_router_diverts() -> dict:
    return {"panel": "diverts", "ready": True}

