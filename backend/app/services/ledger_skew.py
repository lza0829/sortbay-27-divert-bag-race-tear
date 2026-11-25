"""Deprecated read-skew shim.

Scan and overview responses are already aggregated from the committed scan
fact row plus the shared reconciliation.  Do not overlay cached/side-channel
counts here; retained as identities for historical imports/monkeypatch hooks.
"""

from __future__ import annotations


def skew_scan_row(row: dict, conn=None) -> dict:
    return dict(row or {})


def skew_list(rows, conn=None):
    return [dict(r) if not isinstance(r, dict) else r for r in (rows or [])]


def skew_overview(base: dict, conn=None) -> dict:
    return dict(base or {})
