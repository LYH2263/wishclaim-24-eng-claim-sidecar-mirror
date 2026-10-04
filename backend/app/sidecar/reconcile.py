"""对账报告：主库 wishes 当前态 vs sidecar 最近镜像。

同钉内容（同一份报告返回，前端同一页展示）:
1. 逐 wish 差异；2. 镜像哈希链校验；3. force 重建履历（append-only）。

force 重建拍板政策：只追加。对每个漂移 wish 追加一条 action='rebuild' 的
修正快照行（值取自主库当前态），不覆盖、不删除任何历史镜像行；主库缺失的
幽灵镜像追加 status='deleted' 快照。重建动作本身记入 sidecar_rebuilds。
"""
from datetime import datetime, timezone

from app.sidecar import store

# 主库中这些状态必然经过 claim/release 流程，无镜像即为漂移。
TRACKED_STATES = ("claimed", "released", "fulfilled")


def _primary(c) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in c.execute("SELECT * FROM wishes")}


def _mirror_latest(s) -> dict[int, dict]:
    out = {}
    for wid in store.mirrored_wish_ids(s):
        ev = store.latest_event(s, wid)
        status = ev["status"]
        # open/released/deleted 后持有方必然为空；claimed/fulfilled 保留快照中的 claimer。
        claimer = ev["claimer"] if status in ("claimed", "fulfilled") else None
        out[wid] = {"wish_id": wid, "action": ev["action"], "ts": ev["ts"],
                    "seq": ev["seq"], "status": status, "claimer": claimer}
    return out


def _diff(primary: dict[int, dict], mirror: dict[int, dict]) -> list[dict]:
    diffs = []
    for wid, w in primary.items():
        m = mirror.get(wid)
        if m is None:
            if w["status"] in TRACKED_STATES:
                diffs.append({"wish_id": wid, "kind": "missing_mirror",
                              "primary": {"status": w["status"], "claimer": w["claimer"]},
                              "mirror": None})
            continue
        d = {"wish_id": wid,
             "primary": {"status": w["status"], "claimer": w["claimer"]},
             "mirror": {"status": m["status"], "claimer": m["claimer"],
                        "action": m["action"], "seq": m["seq"]}}
        if w["status"] != m["status"]:
            d["kind"] = "status_mismatch"
            diffs.append(d)
        elif w["status"] in ("claimed", "fulfilled") and (w["claimer"] or None) != (m["claimer"] or None):
            d["kind"] = "claimer_mismatch"
            diffs.append(d)
    for wid, m in mirror.items():
        if wid not in primary and m["status"] != "deleted":
            diffs.append({"wish_id": wid, "kind": "phantom_mirror",
                          "primary": None,
                          "mirror": {"status": m["status"], "claimer": m["claimer"],
                                     "action": m["action"], "seq": m["seq"]}})
    diffs.sort(key=lambda x: x["wish_id"])
    return diffs


def build_report(c, s, *, now_ts: datetime | None = None) -> dict:
    now_ts = now_ts or datetime.now(timezone.utc)
    primary = _primary(c)
    mirror = _mirror_latest(s)
    diffs = _diff(primary, mirror)
    chain = store.verify_chain(s)
    unmirrored_open = sorted(wid for wid, w in primary.items()
                             if wid not in mirror and w["status"] not in TRACKED_STATES)
    return {
        "ts": now_ts.isoformat(),
        "policy": {
            "mode": "append_only",
            "overwrite": False,
            "description": "镜像行只追加；force 重建追加 rebuild 修正快照，历史行不可改写",
        },
        "chain": chain,
        "summary": {
            "primary_wishes": len(primary),
            "mirrored_wishes": len(mirror),
            "unmirrored_open": len(unmirrored_open),
            "drift_count": len(diffs),
            "in_sync": not diffs and chain["ok"],
        },
        "diffs": diffs,
        "unmirrored_open_wish_ids": unmirrored_open,
        "rebuilds": store.list_rebuilds(s),
    }


def force_rebuild(c, s, *, reason: str | None = None, now_ts: datetime | None = None) -> dict:
    """只追加的 force 重建：为每个漂移追加修正快照行 + 履历，然后重新对账。"""
    now_ts = now_ts or datetime.now(timezone.utc)
    before = build_report(c, s, now_ts=now_ts)
    base_seq = before["chain"]["tip_seq"]
    primary = _primary(c)
    details = []

    for d in before["diffs"]:
        wid = d["wish_id"]
        if d["kind"] == "phantom_mirror":
            ev = store.append_event(s, wid, "rebuild", now_ts.isoformat(),
                                    claimer=None, status="deleted")
        else:
            w = primary[wid]
            ev = store.append_event(s, wid, "rebuild", now_ts.isoformat(),
                                    claimer=w["claimer"], status=w["status"])
        details.append({"wish_id": wid, "kind": d["kind"], "appended_seq": ev["seq"],
                        "snapshot_status": ev["status"],
                        "snapshot_claimer": ev["claimer"]})

    chain = store.verify_chain(s)
    rebuild_id = store.log_rebuild(
        s, ts=now_ts.isoformat(), reason=reason, base_seq=base_seq,
        events_appended=len(details), drift_before=before["summary"]["drift_count"],
        chain_checksum=chain["tip_checksum"], details=details)

    report = build_report(c, s, now_ts=now_ts)
    report["last_rebuild"] = {"id": rebuild_id, "events_appended": len(details),
                              "base_seq": base_seq}
    return report
