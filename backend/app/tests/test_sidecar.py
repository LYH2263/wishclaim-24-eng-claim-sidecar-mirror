"""Sidecar 镜像仓测试：哈希链、只追加、对账、force 重建、干净流程连跑两次校验和稳定。"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.db import connect as primary_connect
from app.sidecar import dualwrite, reconcile, store

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SIDECAR_DIR", str(tmp_path))
    from app import seed
    seed.init_db()
    c = primary_connect()
    c.execute("DELETE FROM wishes")  # 去掉 seed 的过期样例，保留表结构与 settings
    c.commit()
    s = store.connect(); store.init_db(s)
    yield c, s
    c.close(); s.close()


def _mk_wish(c, title="t", status="open", claimer=None,
             claimed_at=None, expires_at=None):
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        (title, "", status, claimer, claimed_at, expires_at, "clean"))
    c.commit()
    return cur.lastrowid


def _events(s, wid):
    return store.events_for(s, wid)


# ---- 双写 ----

def test_claim_appends_mirror_row(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    res = dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    ev = res["event"]
    assert ev["action"] == "claim" and ev["claimer"] == "alice"
    assert ev["status"] == "claimed" and ev["seq"] == 1
    assert len(ev["checksum"]) == 64 and ev["prev_hash"] == store.GENESIS
    assert store.verify_chain(s)["ok"]


def test_release_appends_and_keeps_history(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    dualwrite.apply_release(c, s, wid, NOW + timedelta(minutes=5))
    rows = _events(s, wid)
    assert [r["action"] for r in rows] == ["claim", "release"]
    # 主库再次 release/claim 不得改写历史镜像行
    before = [dict(r) for r in rows]
    dualwrite.apply_claim(c, s, wid, "bob", NOW + timedelta(minutes=10), 3600)
    dualwrite.apply_release(c, s, wid, NOW + timedelta(minutes=15))
    after = _events(s, wid)
    assert [dict(r) for r in after[:2]] == before
    assert [r["claimer"] for r in after] == ["alice", "alice", "bob", "bob"]
    assert store.verify_chain(s)["ok"]


def test_ttl_sweep_is_mirrored(db_pair):
    c, s = db_pair
    wid = _mk_wish(c, status="claimed", claimer="ghost",
                   claimed_at="2020-01-01T00:00:00+00:00",
                   expires_at="2020-01-01T01:00:00+00:00")
    ids = dualwrite.sweep_expired(c, s, NOW)
    assert ids == [wid]
    ev = store.latest_event(s, wid)
    assert ev["action"] == "ttl_release" and ev["claimer"] == "ghost"
    assert ev["status"] == "open"
    assert store.verify_chain(s)["ok"]


# ---- 哈希链：篡改必破 ----

def test_tampering_breaks_chain(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    dualwrite.apply_release(c, s, wid, NOW)
    s.execute("UPDATE sidecar_events SET claimer='mallory' WHERE seq=1")
    s.commit()
    v = store.verify_chain(s)
    assert v["ok"] is False
    assert any(b["seq"] == 1 and b["reason"] == "checksum_mismatch" for b in v["breaks"])
    # 改第一行还会让第二行 prev_hash 校验不受影响但……链在断裂点之后的链接仍连续：
    assert any(b["reason"] == "checksum_mismatch" for b in v["breaks"])


def test_rewriting_history_breaks_chain(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    dualwrite.apply_claim(c, s, _mk_wish(c), "carol", NOW, 3600)
    # 尝试“改写历史”（系统自身永不这么做）：删除并重插 seq=1
    s.execute("DELETE FROM sidecar_events WHERE seq=1")
    s.commit()
    v = store.verify_chain(s)
    assert v["ok"] is False
    assert any(b["reason"] == "prev_hash_mismatch" for b in v["breaks"])


# ---- 对账 ----

def test_clean_flow_reconciles_in_sync(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    dualwrite.apply_release(c, s, wid, NOW)
    rep = reconcile.build_report(c, s, now_ts=NOW)
    assert rep["summary"]["in_sync"] is True
    assert rep["diffs"] == []
    assert rep["policy"]["mode"] == "append_only"


def test_drift_status_mismatch_detected(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    # 主库被旁路改写（未经双写） -> 漂移
    c.execute("UPDATE wishes SET status='released', claimer=NULL WHERE id=?", (wid,))
    c.commit()
    rep = reconcile.build_report(c, s, now_ts=NOW)
    assert rep["summary"]["drift_count"] == 1
    assert rep["diffs"][0]["kind"] == "status_mismatch"


def test_missing_mirror_and_phantom(db_pair):
    c, s = db_pair
    # 主库 claimed 但无镜像
    wid = _mk_wish(c, status="claimed", claimer="zoe",
                   claimed_at=NOW.isoformat(), expires_at=(NOW + timedelta(hours=1)).isoformat())
    rep = reconcile.build_report(c, s, now_ts=NOW)
    kinds = {d["kind"] for d in rep["diffs"]}
    assert "missing_mirror" in kinds
    # 幽灵镜像
    store.append_event(s, 999, "claim", NOW.isoformat(), claimer="x", status="claimed")
    rep = reconcile.build_report(c, s, now_ts=NOW)
    assert any(d["kind"] == "phantom_mirror" and d["wish_id"] == 999 for d in rep["diffs"])


# ---- force 重建：只追加 + 履历 + 对账同钉 ----

def test_force_rebuild_is_append_only(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    c.execute("UPDATE wishes SET status='fulfilled', claimer='alice' WHERE id=?", (wid,))
    c.commit()
    pre_rows = [dict(r) for r in s.execute("SELECT * FROM sidecar_events ORDER BY seq")]
    rep = reconcile.force_rebuild(c, s, reason="人工核对", now_ts=NOW)
    post_rows = [dict(r) for r in s.execute("SELECT * FROM sidecar_events ORDER BY seq")]
    # 旧行原封不动
    assert post_rows[:len(pre_rows)] == pre_rows
    new = post_rows[len(pre_rows):]
    assert len(new) == 1 and new[0]["action"] == "rebuild"
    assert new[0]["status"] == "fulfilled"
    assert rep["summary"]["in_sync"] is True
    assert len(rep["rebuilds"]) == 1
    rb = rep["rebuilds"][0]
    assert rb["mode"] == "append" and rb["drift_before"] == 1
    assert rb["reason"] == "人工核对"
    assert rb["chain_checksum"] == rep["chain"]["tip_checksum"]
    assert store.verify_chain(s)["ok"]


def test_rebuild_idempotent_when_in_sync(db_pair):
    c, s = db_pair
    wid = _mk_wish(c)
    dualwrite.apply_claim(c, s, wid, "alice", NOW, 3600)
    rep = reconcile.force_rebuild(c, s, now_ts=NOW)
    assert rep["last_rebuild"]["events_appended"] == 0
    assert rep["summary"]["in_sync"] is True


# ---- 干净流程连跑两次，镜像链校验和稳定（确定性） ----

def _clean_run(tmp_path, env):
    c = primary_connect()
    s = store.connect(); store.init_db(s)
    fixed = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("礼物", "", "open", "clean")).lastrowid
    c.commit()
    dualwrite.apply_claim(c, s, wid, "alice", fixed, 3600)
    dualwrite.apply_release(c, s, wid, fixed + timedelta(minutes=2))
    dualwrite.apply_claim(c, s, wid, "bob", fixed + timedelta(minutes=4), 3600)
    tip = store.verify_chain(s)["tip_checksum"]
    seq_rows = [(r["seq"], r["wish_id"], r["action"], r["claimer"], r["status"],
                 r["prev_hash"], r["checksum"])
                for r in s.execute("SELECT * FROM sidecar_events ORDER BY seq")]
    c.close(); s.close()
    return tip, seq_rows


def test_two_clean_runs_produce_stable_checksum(tmp_path, monkeypatch):
    runs = []
    for i in range(2):
        d = tmp_path / f"run{i}"
        monkeypatch.setenv("DATA_DIR", str(d))
        monkeypatch.setenv("SIDECAR_DIR", str(d))
        from app import seed
        seed.init_db()
        c0 = primary_connect(); c0.execute("DELETE FROM wishes"); c0.commit(); c0.close()
        runs.append(_clean_run(d, None))
    assert runs[0] == runs[1]
    assert len(runs[0][1]) == 3
