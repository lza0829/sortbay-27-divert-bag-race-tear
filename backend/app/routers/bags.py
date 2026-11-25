from __future__ import annotations

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
from app.services.flow import add_to_bag as add_to_bag_service
from app.services.flow import open_bag as open_bag_service
from app.services.flow import seal_bag as seal_bag_service

__legacy_symbols__ = (HTTPException, Field, current_shift, require_unsealed)

router = APIRouter(tags=["封袋"])


class BagOpenIn(BaseModel):
    口门编号: int = Field(
        validation_alias=AliasChoices("口门编号", "chute_id", "chuteId")
    )
    备注: str = Field(
        default="",
        validation_alias=AliasChoices("备注", "note", "remark"),
    )


class BagAddIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    扫描编号: int = Field(
        validation_alias=AliasChoices("扫描编号", "scan_id", "scanId")
    )
    # 仅测试夹具在 SORTBAY_ENABLE_FAULTS=1 或 pytest 运行中使用。
    故障点: object | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "故障点", "故障", "注入故障", "失败点",
            "fault", "fault_point", "failure_point", "inject_fault",
        ),
    )


@router.get("/封袋")
@router.get("/袋册")
@router.get("/bags")
def list_bags(shift_id: int | None = None, 班次编号: int | None = None):
    target_shift = shift_id if shift_id is not None else 班次编号
    with db(write=False) as conn:
        sql = """
            SELECT b.*, c.code AS 口门码,
              (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
        """
        params: tuple = ()
        if target_shift is not None:
            sql += " WHERE b.shift_id=?"
            params = (target_shift,)
        sql += " ORDER BY b.id DESC LIMIT 100"
        return rows_to_list(conn.execute(sql, params).fetchall())


@router.get("/封袋/{bag_id}")
@router.get("/袋册/{bag_id}")
@router.get("/bags/{bag_id}")
def get_bag(bag_id: int):
    with db(write=False) as conn:
        bag = conn.execute(
            """
            SELECT b.*, c.code AS 口门码,
                   (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            WHERE b.id=?
            """,
            (bag_id,),
        ).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        result = row_to_dict(bag)
        bag_items = rows_to_list(conn.execute(
            """
            SELECT s.id, s.barcode, s.status, s.dest_code, s.version
            FROM scans s
            WHERE s.bag_id=?
            ORDER BY s.id
            """,
            (bag_id,),
        ).fetchall())
        receipts = rows_to_list(conn.execute(
            "SELECT * FROM operation_receipts WHERE bag_id=? ORDER BY id DESC",
            (bag_id,),
        ).fetchall())
        result["袋件"] = bag_items
        result["袋册成员"] = bag_items
        result["成员"] = bag_items
        result["members"] = bag_items
        result["scans"] = bag_items
        result["明细"] = bag_items
        result["items"] = bag_items
        result["回执"] = receipts
        result["receipts"] = receipts
        return result


@router.post("/封袋/开袋")
@router.post("/集包/开袋")
@router.post("/bags/open")
def open_bag(body: BagOpenIn):
    return open_bag_service(
        body.口门编号,
        body.备注,
        audit_func=audit,
        db_context=db,
        require_func=require_unsealed,
        shift_func=current_shift,
    )


@router.post("/封袋/{bag_id}/入袋")
@router.post("/集包/{bag_id}/入袋")
@router.post("/bags/{bag_id}/add")
@router.post("/bags/{bag_id}/items")
def add_to_bag(bag_id: int, body: BagAddIn):
    fault = extract_fault(body)
    return add_to_bag_service(
        bag_id,
        body.扫描编号,
        fault=fault,
        audit_func=audit,
        db_context=db,
        now_func=now_str,
        require_func=require_unsealed,
        shift_func=current_shift,
    )


@router.post("/封袋/{bag_id}/封口")
@router.post("/集包/{bag_id}/封口")
@router.post("/bags/{bag_id}/seal")
def seal_bag(bag_id: int):
    return seal_bag_service(
        bag_id,
        audit_func=audit,
        db_context=db,
        now_func=now_str,
        require_func=require_unsealed,
        shift_func=current_shift,
    )

