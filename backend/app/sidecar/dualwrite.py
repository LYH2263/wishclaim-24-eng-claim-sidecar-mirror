"""双写适配：主库 wishes 变更与 sidecar 镜像行追加在同一业务流程内完成。

约定：主库 UPDATE 先提交，镜像行随后追加（各自独立 SQLite 文件，无法共享事务）。
若两步之间崩溃，对账入口 (/api/audit/reconcile) 会暴露漂移，并可用 append-only
force 重建追加修正快照——历史镜像行永不被改写。
"""
from datetime import datetime

from app.engines.claim_lock import claim_allowed, lock_payload, release_if_expired
from app.sidecar import store


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class BadState(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def sweep_expired(c, s, now_ts: datetime) -> list[int]:
    """TTL 过期回收同样属于 release，必须镜像 ttl_release 行。返回被回收的 wish_id。"""
    released = []
    for r in c.execute("SELECT * FROM wishes WHERE status='claimed'"):
        rel = release_if_expired(r["status"], r["expires_at"], now_ts)
        if not rel:
            continue
        c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
                  (rel["status"], None, None, None, r["id"]))
        store.append_event(s, r["id"], "ttl_release", now_ts.isoformat(),
                           claimer=r["claimer"], status=rel["status"])
        released.append(r["id"])
    return released


def apply_claim(c, s, wid: int, claimer: str, now_ts: datetime, ttl_seconds: int) -> dict:
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r:
        raise NotFound()
    allowed = claim_allowed(r["status"], r["claimer"], now_ts, r["expires_at"])
    if not allowed["ok"]:
        raise Conflict(allowed["reason"])
    p = lock_payload(claimer, now_ts, ttl_seconds)
    c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
              (p["status"], p["claimer"], p["claimed_at"], p["expires_at"], wid))
    c.commit()
    event = store.append_event(s, wid, "claim", p["claimed_at"],
                               claimer=claimer, status="claimed")
    return {"lock": p, "event": event, "reason": allowed["reason"]}


def apply_release(c, s, wid: int, now_ts: datetime) -> dict:
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r:
        raise NotFound()
    if r["status"] != "claimed":
        raise BadState("not_claimed")
    holder = r["claimer"]
    c.execute("UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, expires_at=NULL"
              " WHERE id=?", (wid,))
    c.commit()
    # claimer 记录被释放的持有方（动作履历），释放后的期望态以 status='released' 为准。
    event = store.append_event(s, wid, "release", now_ts.isoformat(),
                               claimer=holder, status="released")
    return {"event": event}


def apply_fulfill(c, s, wid: int, now_ts: datetime) -> dict:
    """fulfill 不是需求点名的双写动作，但不镜像会让对账永久假差异，故一并追加。"""
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r:
        raise NotFound()
    if r["status"] != "claimed":
        raise BadState("need_claim")
    c.execute("UPDATE wishes SET status='fulfilled' WHERE id=?", (wid,))
    c.commit()
    event = store.append_event(s, wid, "fulfill", now_ts.isoformat(),
                               claimer=r["claimer"], status="fulfilled")
    return {"event": event}
