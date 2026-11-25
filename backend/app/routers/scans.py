from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from app.db import db, row_to_dict, rows_to_list
from app.services.common import audit, current_shift, now_str, outcome_lights, require_unsealed
from app.services.flow import create_scan as create_scan_service
from app.services.flow import route_scan as route_scan_service

# Preserve historical router-module symbols for white-box monkeypatch fixtures.
__legacy_symbols__ = (
    HTTPException, row_to_dict, current_shift, require_unsealed, Field,
)

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    条码: str = Field(
        min_length=1,
        validation_alias=AliasChoices("条码", "barcode", "code"),
    )
    目的地码: str = Field(
        default="",
        validation_alias=AliasChoices(
            "目的地码", "dest_code", "destination_code", "dest"
        ),
    )
    分拣线编号: int | None = Field(
        default=None,
        validation_alias=AliasChoices("分拣线编号", "line_id", "lineId"),
    )
    备注: str = Field(
        default="",
        validation_alias=AliasChoices("备注", "note", "remark"),
    )


@router.get("/扫描")
@router.get("/扫描终态")
@router.get("/scans")
@router.get("/scan-states")
def list_scans(
    状态: str | None = None,
    status: str | None = None,
    barcode: str | None = None,
    条码: str | None = None,
    code: str | None = None,
    shift_id: int | None = None,
    班次编号: int | None = None,
):
    status_value = 状态 or status
    barcode_value = barcode or 条码 or code
    if barcode_value is not None:
        barcode_value = str(barcode_value).strip() or None
    target_shift = shift_id if shift_id is not None else 班次编号
    status_aliases = {
        "pending": "待路由",
        "created": "待路由",
        "routed": "已路由",
        "diverted": "错分回流",
        "divert": "错分回流",
        "bagged": "已入袋",
        "in_bag": "已入袋",
        "sealed": "已封袋",
    }
    if status_value:
        status_value = status_aliases.get(
            str(status_value).strip().lower(), status_value
        )
    with db(write=False) as conn:
        where = []
        params: list[object] = []
        if status_value:
            where.append("status=?")
            params.append(status_value)
        if barcode_value:
            where.append("barcode=?")
            params.append(barcode_value)
        if target_shift is not None:
            where.append("shift_id=?")
            params.append(target_shift)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            f"SELECT * FROM scans{where_sql} ORDER BY id DESC LIMIT 200",
            tuple(params),
        ).fetchall()
        result = rows_to_list(rows)
        for row in result:
            row.update(outcome_lights(
                row.get("status"), row.get("bag_id"), row.get("divert_batch_id")
            ))
        return result


@router.get("/扫描/{scan_id}")
@router.get("/扫描终态/{scan_id}")
@router.get("/扫描册/{scan_id}")
@router.get("/scans/{scan_id}")
def get_scan(scan_id: int):
    with db(write=False) as conn:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not row:
            raise HTTPException(404, "扫描不存在")
        result = row_to_dict(row)
        result.update(outcome_lights(
            result.get("status"), result.get("bag_id"), result.get("divert_batch_id")
        ))
        result["路由事件"] = rows_to_list(conn.execute(
            "SELECT * FROM route_events WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall())
        result["route_events"] = result["路由事件"]
        result["回执"] = rows_to_list(conn.execute(
            "SELECT * FROM operation_receipts WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall())
        result["receipts"] = result["回执"]
        result["回流明细"] = rows_to_list(conn.execute(
            "SELECT * FROM divert_items WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall())
        return result


# 扫描册是同一事实面的页面/别名，避免另一套列表口径。
@router.get("/扫描册")
def list_scan_book(
    状态: str | None = None,
    status: str | None = None,
    barcode: str | None = None,
    条码: str | None = None,
    code: str | None = None,
    shift_id: int | None = None,
    班次编号: int | None = None,
):
    return list_scans(
        状态=状态,
        status=status,
        barcode=barcode,
        条码=条码,
        code=code,
        shift_id=shift_id,
        班次编号=班次编号,
    )


@router.post("/扫描")
@router.post("/装载")
@router.post("/scans")
@router.post("/loading")
def create_scan(body: ScanIn):
    return create_scan_service(
        barcode=body.条码,
        dest_code=body.目的地码,
        line_id=body.分拣线编号,
        note=body.备注,
        audit_func=audit,
        db_context=db,
        now_func=now_str,
        require_func=require_unsealed,
        shift_func=current_shift,
    )


@router.post("/扫描/{scan_id}/路由")
@router.post("/扫描/{scan_id}/落入")
@router.post("/scans/{scan_id}/route")
@router.post("/scans/{scan_id}/land")
def route_scan(scan_id: int):
    return route_scan_service(
        scan_id,
        audit_func=audit,
        db_context=db,
        now_func=now_str,
        require_func=require_unsealed,
        shift_func=current_shift,
    )

