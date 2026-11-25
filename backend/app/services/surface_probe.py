"""Deprecated compatibility probe.

Surface claims are exposed through the reconciled overview itself; there is no
separate probe state.  Keep the callable for old imports but never synthesize
an OK panel from abandoned settings keys.
"""

from __future__ import annotations


def probe_panel(conn) -> dict:
    return {"ok": False, "last": {}}


def merge_probe(base: dict, conn) -> dict:
    return dict(base or {})
