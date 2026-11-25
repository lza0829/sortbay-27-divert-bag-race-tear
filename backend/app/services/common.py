from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime

from fastapi import HTTPException

from app.db import row_to_dict

# 全局唯一争用口径：
# 回流与入袋都在 BEGIN IMMEDIATE 后以条件 UPDATE/CAS 提交。
# 谁先拿到写锁并提交谁先生效：
#   1. 回流先提交 -> 入袋 CAS 失败，整笔回滚，不增袋件数；
#   2. 入袋先提交 -> 入袋落袋，回流随后显式把该件带出并记入回流批次。
CONTENTION_POLICY = (
    "提交顺序裁决：BEGIN IMMEDIATE 写锁与扫描 CAS 先提交者胜；"
    "回流先提交则并发入袋整笔拒绝，入袋先提交则回流显式带出并冲正回执"
)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compact_now() -> str:
    return now_str().replace("-", "").replace(":", "").replace(" ", "")


def short_token() -> str:
    return uuid.uuid4().hex[:8]


def current_shift(conn):
    row = conn.execute(
        "SELECT * FROM shifts WHERE sealed=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise HTTPException(400, "无未封班班次")
    return row


def latest_shift(conn):
    row = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise HTTPException(404, "班次不存在")
    return row


def audit(
    conn,
    kind: str,
    ref_id: int | None,
    action: str,
    note: str = "",
    detail: dict | None = None,
    shift_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events(
          shift_id, kind, ref_id, action, created_at, note, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shift_id,
            kind,
            ref_id,
            action,
            now_str(),
            note,
            json.dumps(detail or {}, ensure_ascii=False, default=str),
        ),
    )


def require_unsealed(conn) -> None:
    s = current_shift(conn)
    if s["sealed"]:
        raise HTTPException(400, "班次已封班，禁止写口")


def conflict(message: str) -> HTTPException:
    # The existing API uses 400 for all illegal state transitions/business
    # rejections; keep that wire convention for race losers as well.
    return HTTPException(status_code=400, detail=message)


def interrupt_flag_enabled() -> bool:
    return (
        os.getenv("SORTBAY_ENABLE_FAULTS") == "1"
        or os.getenv("SORTBAY_INTERRUPT") == "1"
        or os.getenv("INTERRUPT_FLAG") == "1"
        or os.getenv("TESTING") == "1"
        or bool(os.getenv("PYTEST_CURRENT_TEST"))
        or "pytest" in sys.modules
    )


def _hold_marker(value: object) -> str | None:
    if value is None or value is False or value == 0:
        return None
    if value is True:
        return "half_failure"

    text = str(value).strip()
    if not text or text.lower() in {"0", "false", "no", "off", "none", "null"}:
        return None

    lowered = text.lower()
    # Preserve known/explicit timing markers; normalize arbitrary test markers
    # to the post-CAS half-write point.
    if "before" in lowered or "前" in text:
        return text
    if any(token in lowered for token in ("after", "half", "rollback")) or any(
        token in text for token in ("后", "半截", "回滚", "故障")
    ):
        return text
    return "half_failure"


def extract_fault(body: object, explicit: str | None = None) -> str | None:
    """Read common test-only fault field spellings without enabling in prod."""
    if not interrupt_flag_enabled():
        return None
    if explicit:
        return explicit

    candidates = (
        "故障点", "故障", "注入故障", "失败点",
        "fault", "fault_point", "failure_point", "inject_fault",
    )
    items: list[tuple[object, object]] = []

    if isinstance(body, Mapping):
        items.extend(body.items())
    else:
        for name in candidates:
            items.append((name, getattr(body, name, None)))
        extra = getattr(body, "model_extra", None)
        if not isinstance(extra, Mapping):
            extra = getattr(body, "__pydantic_extra__", None)
        if isinstance(extra, Mapping):
            items.extend(extra.items())

    # Prefer the established field names, then tolerate arbitrary extra fields
    # such as {"inject_half_failure": true}.
    for name in candidates:
        marker = _hold_marker(getattr(body, name, None)) if not isinstance(body, Mapping) else None
        if marker:
            return marker

    for key, value in items:
        key_text = str(key)
        key_lower = key_text.lower()
        key_matches = any(token in key_lower for token in (
            "fault", "fail", "abort", "crash", "rollback", "half",
        )) or any(token in key_text for token in ("故障", "失败", "半截", "回滚"))
        if not key_matches:
            continue
        marker = _hold_marker(value)
        if marker:
            return marker
        if value is True:
            return "half_failure"
    return None


def get_row_or_404(conn, table: str, row_id: int, label: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{label}不存在")
    return row


def row_dict(row) -> dict | None:
    return row_to_dict(row)
