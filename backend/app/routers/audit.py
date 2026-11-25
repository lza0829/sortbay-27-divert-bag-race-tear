from __future__ import annotations

import json
import re

from fastapi import APIRouter

from app.db import db, rows_to_list
from app.services.reconcile import replay_audit

router = APIRouter(tags=["审计"])


def _parse_detail(raw: object) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first_nonempty(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _detail_scan_ids(detail: dict) -> set[int]:
    ids: set[int] = set()
    for key in (
        "scan_id", "scanId", "扫描编号", "扫描id", "扫描ID",
        "target_scan_id", "targetScanId",
    ):
        value = detail.get(key)
        if value is None:
            continue
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            pass

    for key in ("scan_ids", "scanIds", "扫描编号列表"):
        value = detail.get(key)
        if isinstance(value, list):
            for item in value:
                try:
                    ids.add(int(item))
                except (TypeError, ValueError):
                    pass

    scan_obj = detail.get("scan") or detail.get("扫描")
    if isinstance(scan_obj, dict) and scan_obj.get("id") is not None:
        try:
            ids.add(int(scan_obj["id"]))
        except (TypeError, ValueError):
            pass
    return ids


def _detail_chute_ids(detail: dict) -> set[int]:
    ids: set[int] = set()
    for key in (
        "chute_id", "chuteId", "口门编号", "口门id", "口门ID",
        "target_chute_id", "targetChuteId",
    ):
        parsed = _as_int(detail.get(key))
        if parsed is not None:
            ids.add(parsed)

    for key in ("chute", "口门"):
        obj = detail.get(key)
        if isinstance(obj, dict):
            parsed = _as_int(obj.get("id") or obj.get("chute_id"))
            if parsed is not None:
                ids.add(parsed)
        else:
            parsed = _as_int(obj)
            if parsed is not None:
                ids.add(parsed)
    return ids


def _detail_chute_codes(detail: dict) -> set[str]:
    codes: set[str] = set()
    for key in (
        "chute_code", "chuteCode", "口门码", "口门编码", "chute",
    ):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            codes.add(value.strip())

    for key in ("chute", "口门"):
        obj = detail.get(key)
        if isinstance(obj, dict):
            for code_key in ("code", "chute_code", "chuteCode", "口门码"):
                value = obj.get(code_key)
                if value is not None and str(value).strip():
                    codes.add(str(value).strip())
    return codes


def _detail_line_ids(detail: dict) -> set[int]:
    ids: set[int] = set()
    for key in ("line_id", "lineId", "分拣线编号", "线体编号"):
        parsed = _as_int(detail.get(key))
        if parsed is not None:
            ids.add(parsed)
    line_obj = detail.get("line") or detail.get("分拣线")
    if isinstance(line_obj, dict):
        parsed = _as_int(line_obj.get("id") or line_obj.get("line_id"))
        if parsed is not None:
            ids.add(parsed)
    return ids


def _detail_line_names(detail: dict) -> set[str]:
    names: set[str] = set()
    for key in (
        "line_name", "lineName", "分拣线名称", "分拣线名", "线体", "line",
    ):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    line_obj = detail.get("line") or detail.get("分拣线")
    if isinstance(line_obj, dict):
        for name_key in ("name", "line_name", "lineName", "分拣线名称"):
            value = line_obj.get(name_key)
            if value is not None and str(value).strip():
                names.add(str(value).strip())
    return names


def _audit_matches_scan(row: dict, detail: dict, scan_id: int) -> bool:
    if scan_id in _detail_scan_ids(detail):
        return True
    if row.get("kind") == "扫描" and row.get("ref_id") == scan_id:
        return True

    note = str(row.get("note") or "")
    if note == str(scan_id):
        return True
    # Require a non-digit boundary so 查询扫描1 does not bleed into 扫描10/11.
    return re.search(rf"(?:扫描|scan)\s*[:：#]?\s*{scan_id}(?!\d)", note, re.I) is not None


def _audit_matches_barcode(
    row: dict,
    detail: dict,
    barcode: str,
    scan_barcodes: dict[int, str],
) -> bool:
    wanted = barcode.strip()
    if not wanted:
        return True
    for key in ("barcode", "条码", "code"):
        value = detail.get(key)
        if value is not None and str(value) == wanted:
            return True
    if str(row.get("note") or "") == wanted:
        return True
    for sid in _detail_scan_ids(detail):
        if scan_barcodes.get(sid) == wanted:
            return True
    return False


@router.get("/审计")
@router.get("/审计/查询")
@router.get("/audit")
@router.get("/audit/query")
@router.get("/audits")
def list_audit(
    limit: int = 200,
    scan_id: int | None = None,
    scanId: int | None = None,
    扫描编号: int | None = None,
    kind: str | None = None,
    类型: str | None = None,
    action: str | None = None,
    动作: str | None = None,
    barcode: str | None = None,
    条码: str | None = None,
    code: str | None = None,
    line_id: int | None = None,
    lineId: int | None = None,
    分拣线编号: int | None = None,
    line_name: str | None = None,
    lineName: str | None = None,
    分拣线名称: str | None = None,
    分拣线名: str | None = None,
    分拣线: str | None = None,
    line: str | None = None,
    dest_code: str | None = None,
    destination_code: str | None = None,
    destination: str | None = None,
    dest: str | None = None,
    目的地码: str | None = None,
    chute_id: str | None = None,
    chuteId: str | None = None,
    口门编号: str | None = None,
    chute_code: str | None = None,
    chuteCode: str | None = None,
    口门码: str | None = None,
    chute: str | None = None,
    口门: str | None = None,
):
    limit = max(1, min(limit, 1000))
    target_kind = kind or 类型
    target_action = action or 动作
    target_scan = scan_id if scan_id is not None else (
        scanId if scanId is not None else 扫描编号
    )
    target_barcode = barcode or 条码 or code
    if target_barcode is not None:
        target_barcode = str(target_barcode).strip() or None
    target_line_id = (
        line_id if line_id is not None
        else lineId if lineId is not None
        else 分拣线编号
    )
    target_line_name = _first_nonempty(
        line_name, lineName, 分拣线名称, 分拣线名,
    )
    generic_line = _first_nonempty(line, 分拣线)
    if target_line_id is None and generic_line is not None:
        numeric_line = _as_int(generic_line)
        if numeric_line is not None:
            target_line_id = numeric_line
        elif target_line_name is None:
            target_line_name = generic_line

    target_dest = _first_nonempty(
        dest_code, destination_code, destination, dest, 目的地码,
    )

    target_chute_id = None
    for value in (chute_id, chuteId, 口门编号, chute, 口门):
        target_chute_id = _as_int(value)
        if target_chute_id is not None:
            break
    target_chute_code = _first_nonempty(
        chute_code,
        chuteCode,
        口门码,
        None if _as_int(chute_id) is not None else chute_id,
        None if _as_int(chuteId) is not None else chuteId,
        None if _as_int(口门编号) is not None else 口门编号,
        None if _as_int(chute) is not None else chute,
        None if _as_int(口门) is not None else 口门,
    )

    with db(write=False) as conn:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list[object] = []
        if target_kind:
            sql += " AND kind=?"
            params.append(target_kind)
        if target_action:
            sql += " AND action=?"
            params.append(target_action)

        # Coarse SQL prefilters only.  Exact scan/barcode membership is decided
        # after JSON parsing below, avoiding prefix matches such as scan 1/10.
        if target_scan is not None:
            sid_int = int(target_scan)
            sql += """
              AND (
                (kind='扫描' AND ref_id=?)
                OR detail_json LIKE ?
                OR note = ?
                OR note LIKE ?
              )
            """
            params.extend((
                target_scan,
                f"%{sid_int}%",
                str(target_scan),
                f"%扫描{sid_int}%",
            ))
        # Barcode/line/destination exact filtering is performed after JSON
        # parsing/scan-row resolution below.

        sql += " ORDER BY id DESC"
        # Exact Python filtering is required when an entity filter is present;
        # otherwise SQL LIMIT can truncate before the requested rows are found.
        needs_python_filter = any((
            target_scan is not None,
            target_barcode is not None,
            target_line_id is not None,
            target_line_name is not None,
            target_dest is not None,
            target_chute_id is not None,
            target_chute_code is not None,
        ))
        if not needs_python_filter:
            sql += " LIMIT ?"
            params.append(limit)

        rows = rows_to_list(conn.execute(sql, tuple(params)).fetchall())
        scan_rows = {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT * FROM scans").fetchall()
        }
        chute_rows = {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT * FROM chutes").fetchall()
        }
        line_rows = {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT * FROM lines").fetchall()
        }

        result: list[dict] = []
        for row in rows:
            detail = _parse_detail(row.get("detail_json"))
            row["detail"] = detail
            if row.get("shift_id") is not None:
                row["班次编号"] = row["shift_id"]
            if target_scan is not None and not _audit_matches_scan(
                row, detail, int(target_scan)
            ):
                continue
            if target_barcode is not None and not _audit_matches_barcode(
                row,
                detail,
                target_barcode,
                {sid: scan.get("barcode") for sid, scan in scan_rows.items()},
            ):
                continue
            scan_ids = _detail_scan_ids(detail)
            related_scans = [
                scan_rows[sid] for sid in scan_ids if sid in scan_rows
            ]

            if target_line_id is not None or target_line_name is not None:
                line_ids = _detail_line_ids(detail)
                line_ids.update(
                    int(scan["line_id"])
                    for scan in related_scans
                    if scan.get("line_id") is not None
                )
                line_names = _detail_line_names(detail)
                line_names.update(
                    str(line_rows[lid].get("name") or "")
                    for lid in line_ids
                    if lid in line_rows
                )
                line_matches = True
                if target_line_id is not None:
                    line_matches = target_line_id in line_ids
                if target_line_name is not None:
                    line_matches = (
                        line_matches and target_line_name in line_names
                    )
                if not line_matches:
                    continue

            if target_chute_id is not None or target_chute_code is not None:
                chute_ids = _detail_chute_ids(detail)
                if row.get("kind") in {"口门", "chute", "Chute"}:
                    parsed_ref = _as_int(row.get("ref_id"))
                    if parsed_ref is not None:
                        chute_ids.add(parsed_ref)
                chute_ids.update(
                    int(scan["chute_id"])
                    for scan in related_scans
                    if scan.get("chute_id") is not None
                )
                chute_codes = _detail_chute_codes(detail)
                chute_codes.update(
                    str(chute_rows[cid].get("code") or "")
                    for cid in chute_ids
                    if cid in chute_rows
                )
                chute_matches = True
                if target_chute_id is not None:
                    chute_matches = target_chute_id in chute_ids
                if target_chute_code is not None:
                    chute_matches = (
                        chute_matches and target_chute_code in chute_codes
                    )
                if not chute_matches:
                    continue

            if target_dest is not None:
                detail_dest = _first_nonempty(
                    detail.get("dest_code"),
                    detail.get("destination_code"),
                    detail.get("destination"),
                    detail.get("dest"),
                    detail.get("目的地码"),
                )
                scan_dests = {
                    str(scan.get("dest_code") or "")
                    for scan in related_scans
                }
                if (
                    target_dest != detail_dest
                    and target_dest not in scan_dests
                ):
                    continue
            result.append(row)
            if len(result) >= limit:
                break
        return result


@router.get("/审计/重放")
@router.get("/审计/replay")
@router.get("/audit/replay")
def audit_replay(
    shift_id: int | None = None,
    scan_id: int | None = None,
    scanId: int | None = None,
    扫描编号: int | None = None,
    班次编号: int | None = None,
):
    target_scan = scan_id if scan_id is not None else (
        scanId if scanId is not None else 扫描编号
    )
    target_shift = shift_id if shift_id is not None else 班次编号
    with db(write=False) as conn:
        return replay_audit(conn, shift_id=target_shift, scan_id=target_scan)


@router.get("/回执")
@router.get("/receipts")
def list_receipts(
    scan_id: int | None = None,
    scanId: int | None = None,
    扫描编号: int | None = None,
    barcode: str | None = None,
    条码: str | None = None,
    code: str | None = None,
):
    target_scan = scan_id if scan_id is not None else (
        scanId if scanId is not None else 扫描编号
    )
    target_barcode = _first_nonempty(barcode, 条码, code)
    sql = """
      SELECT r.*, s.shift_id AS shift_id, s.shift_id AS 班次编号
      FROM operation_receipts r
      LEFT JOIN scans s ON s.id=r.scan_id
    """
    where = []
    params_list: list[object] = []
    if target_scan is not None:
        where.append("r.scan_id=?")
        params_list.append(target_scan)
    if target_barcode is not None:
        where.append("s.barcode=?")
        params_list.append(target_barcode)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC LIMIT 200"
    with db(write=False) as conn:
        return rows_to_list(conn.execute(sql, tuple(params_list)).fetchall())
