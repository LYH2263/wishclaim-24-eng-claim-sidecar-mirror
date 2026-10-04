"""Dual-write adapter: every successful claim/release appends a sidecar mirror row
in the SAME SQLite transaction as the primary wishes update.

主库 wishes 更新与 sidecar 镜像追加同事务提交：任一失败则整体回滚，
不会出现「主库已改、镜像缺失」的半成品。
历史镜像行只追加，后续 release/claim 永不改写。
"""
from datetime import datetime, timezone

from app.modules import sidecar_store as sc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_dual():
    """主库连接并 ATTACH sidecar，供整段双写事务使用。"""
    from app.db import connect
    c = connect()
    sc.attach(c)
    return c


def write_event(c, wish_id: int, action: str, status: str, claimer: str | None,
                occurred_at: str | None = None) -> dict:
    """在当前事务内追加镜像行（不提交）。"""
    return sc.append_event(c, wish_id, action, status, claimer,
                           occurred_at or _now_iso())


def apply_claim(c, wish_id: int, payload: dict, occurred_at: str | None = None) -> dict:
    c.execute(
        "UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
        (payload["status"], payload["claimer"], payload["claimed_at"],
         payload["expires_at"], wish_id),
    )
    event = write_event(c, wish_id, "claim", payload["status"], payload["claimer"],
                        occurred_at or payload["claimed_at"])
    return event


def apply_release(c, wish_id: int, status: str = "released", action: str = "release",
                  occurred_at: str | None = None) -> dict:
    c.execute(
        "UPDATE wishes SET status=?, claimer=NULL, claimed_at=NULL, expires_at=NULL WHERE id=?",
        (status, wish_id),
    )
    return write_event(c, wish_id, action, status, None, occurred_at)
