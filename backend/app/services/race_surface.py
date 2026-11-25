"""Deprecated compatibility shim for the former side-channel surface.

The contention outcome now has exactly one source of truth: the committed scan
row, bag membership, divert item/batch and reversal-linked receipts.  These
helpers remain importable for old white-box callers, but they must never paint
independent success lights or persist a parallel ``surface_last`` claim.
"""

from __future__ import annotations

from app.services.common import outcome_lights


def overlay_overview(base: dict, conn=None) -> dict:
    """Return the reconciled overview unchanged."""
    return dict(base or {})


def _truthful_out(out, *args, **kwargs):
    if not isinstance(out, dict):
        return out

    scan = out.get("扫描") or out.get("scan")
    source = scan if isinstance(scan, dict) else out
    status = source.get("status") or source.get("终态") or out.get("status")
    bag_id = source.get("bag_id", out.get("bag_id"))
    batch_id = source.get("divert_batch_id", out.get("divert_batch_id"))
    lights = outcome_lights(status, bag_id, batch_id)

    clean = dict(out)
    # Never leave the old hard-coded dual claims in place.
    clean.pop("surface_ok", None)
    for key in [k for k in clean if str(k).startswith("surface_")]:
        clean.pop(key, None)
    clean.update(lights)
    if isinstance(scan, dict):
        scan.update(lights)
    return clean


def after_divert(out, *args, **kwargs):
    return _truthful_out(out, *args, **kwargs)


def after_bag(out, *args, **kwargs):
    return _truthful_out(out, *args, **kwargs)
