"""Reconciliation report + force rebuild.

对账：主库 wishes 当前态 vs sidecar 最近镜像，逐条列出差异；并重算镜像链校验和。
force 重建策略（已拍板）：只允许 append_only —— 对全部愿望追加当前快照行，
永不覆盖、永不改写最新镜像行；显式要求 overwrite 一律拒绝并在重建履历留痕。
"""
from fastapi import HTTPException

from app.modules import sidecar_store as sc

# 拍板：重建只追加。overwrite 不是「未实现」，而是策略性永久禁止。
REBUILD_MODE_APPEND = "append_only"
REBUILD_MODE_OVERWRITE = "overwrite"
OVERWRITE_REASON = "overwrite_forbidden: mirror ledger is append-only by policy"


def _primary_states(c) -> dict[int, dict]:
    return {
        r["id"]: {"wish_id": r["id"], "status": r["status"], "claimer": r["claimer"]}
        for r in c.execute("SELECT id, status, claimer FROM wishes")
    }


def _mirror_view(row: dict) -> dict:
    return {"wish_id": row["wish_id"], "status": row["status"],
            "claimer": row["claimer"], "seq": row["seq"], "action": row["action"],
            "occurred_at": row["occurred_at"]}


def report(c) -> dict:
    """主库当前态 vs 最近镜像 + 链校验。"""
    chain = sc.verify_chain(c)
    primary = _primary_states(c)
    latest = sc.latest_per_wish(c)

    diffs: list[dict] = []
    for wid, p in sorted(primary.items()):
        m = latest.get(wid)
        if m is None:
            diffs.append({"wish_id": wid, "kind": "missing_mirror",
                          "primary": p, "mirror": None})
            continue
        mv = _mirror_view(m)
        if m["status"] != p["status"]:
            diffs.append({"wish_id": wid, "kind": "status_mismatch",
                          "primary": p, "mirror": mv})
        if (m["claimer"] or None) != (p["claimer"] or None):
            diffs.append({"wish_id": wid, "kind": "claimer_mismatch",
                          "primary": p, "mirror": mv})

    for wid, m in sorted(latest.items()):
        if wid not in primary:
            diffs.append({"wish_id": wid, "kind": "primary_missing",
                          "primary": None, "mirror": _mirror_view(m)})

    return {
        "ok": chain["ok"] and not diffs,
        "chain": chain,
        "wish_count": len(primary),
        "diff_count": len(diffs),
        "diffs": diffs,
        "rebuild_history": sc.rebuild_history(c),
        "policy": {
            "rebuild_mode": REBUILD_MODE_APPEND,
            "overwrite": "forbidden_and_logged",
        },
    }


def rebuild(c, mode: str, now_iso: str) -> dict:
    """force 重建。返回 (report_before, report_after, record)。overwrite 抛 400。"""
    before = report(c)

    if mode == REBUILD_MODE_OVERWRITE:
        rec = sc.record_rebuild(c, REBUILD_MODE_OVERWRITE, "rejected",
                                OVERWRITE_REASON, before["wish_count"],
                                0, now_iso)
        c.commit()
        raise HTTPException(status_code=400, detail={
            "reason": OVERWRITE_REASON,
            "rebuild_seq": rec["rebuild_seq"],
            "note": "拒绝已记入重建履历；镜像行一行未改",
        })
    if mode != REBUILD_MODE_APPEND:
        raise HTTPException(status_code=400, detail=f"unknown_rebuild_mode: {mode}")

    primary = _primary_states(c)
    rows = 0
    for wid, p in primary.items():
        sc.append_event(c, wid, "rebuild_snapshot", p["status"], p["claimer"], now_iso)
        rows += 1
    rec = sc.record_rebuild(c, REBUILD_MODE_APPEND, "ok", None, len(primary), rows, now_iso)
    c.commit()

    after = report(c)
    return {"before": before, "after": after, "rebuild": rec}
