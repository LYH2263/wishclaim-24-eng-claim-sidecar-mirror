import sqlite3
import pytest
from fastapi import HTTPException

from app import seed
from app.modules import sidecar_store as sc
from app.modules import dualwrite
from app.modules import reconcile_report as rr


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return tmp_path


def _claim(c, wid, claimer, ts):
    from app.engines.claim_lock import lock_payload
    return dualwrite.apply_claim(c, wid, lock_payload(claimer, ts, 3600),
                                 occurred_at=ts.isoformat())


def _release(c, wid, ts):
    return dualwrite.apply_release(c, wid, "released", "release", ts.isoformat())


def run_clean_flow(end_ts):
    """一套干净流程：建愿望 → claim(alice) → release → claim(bob) → release。"""
    from datetime import timedelta
    c = dualwrite.connect_dual()
    cur = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean"))
    wid = cur.lastrowid
    checksums = []
    t0 = end_ts
    e1 = _claim(c, wid, "alice", t0); checksums.append(e1["checksum"])
    e2 = _release(c, wid, t0 + timedelta(seconds=10)); checksums.append(e2["checksum"])
    e3 = _claim(c, wid, "bob", t0 + timedelta(seconds=20)); checksums.append(e3["checksum"])
    e4 = _release(c, wid, t0 + timedelta(seconds=30)); checksums.append(e4["checksum"])
    c.commit(); c.close()
    return wid, checksums


def test_clean_flow_twice_checksums_stable(data_dir):
    """连跑两次：相同输入序列 → 镜像链校验和逐位一致，且全链可验证。"""
    from datetime import datetime, timezone
    T = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

    _, cs1 = run_clean_flow(T)
    c1 = dualwrite.connect_dual()
    v1 = sc.verify_chain(c1); c1.close()
    assert v1["ok"]

    # 第二个 DATA_DIR 重放完全相同的序列
    import os, shutil
    d2 = data_dir.parent / "run2"; shutil.rmtree(d2, ignore_errors=True); d2.mkdir()
    os.environ["DATA_DIR"] = str(d2)
    try:
        seed.init_db()
        _, cs2 = run_clean_flow(T)
        c2 = dualwrite.connect_dual()
        v2 = sc.verify_chain(c2); c2.close()
    finally:
        os.environ["DATA_DIR"] = str(data_dir)
    assert cs1 == cs2
    assert v2["ok"]
    assert v2["tail_checksum"] == v1["tail_checksum"]


def test_reclaim_does_not_rewrite_history(data_dir):
    """再次 release/claim 后，历史镜像行原样保留（seq/状态/认领人/时刻/校验和）。"""
    from datetime import datetime, timedelta, timezone
    T = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    c = dualwrite.connect_dual()
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean")).lastrowid
    first = _claim(c, wid, "alice", T)
    rel = _release(c, wid, T + timedelta(seconds=5))
    c.commit()

    _claim(c, wid, "bob", T + timedelta(seconds=10))
    _release(c, wid, T + timedelta(seconds=15))
    c.commit(); c.close()

    c = dualwrite.connect_dual()
    rows = sc.all_events(c)
    assert [e["checksum"] for e in rows[:2]] == [first["checksum"], rel["checksum"]]
    kept = rows[0]
    assert (kept["action"], kept["claimer"], kept["status"], kept["occurred_at"]) == \
           ("claim", "alice", "claimed", T.isoformat())
    assert kept["seq"] == first["seq"]
    c.close()


def test_immutable_storage_blocks_update_and_delete(data_dir):
    c = dualwrite.connect_dual()
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean")).lastrowid
    from datetime import datetime, timezone
    _claim(c, wid, "x", datetime(2026, 3, 1, tzinfo=timezone.utc)); c.commit()
    with pytest.raises(sqlite3.Error, match="immutable"):
        c.execute("UPDATE sidecar.wish_mirror SET claimer='hacker' WHERE seq=1")
    with pytest.raises(sqlite3.Error, match="immutable"):
        c.execute("DELETE FROM sidecar.wish_mirror WHERE seq=1")
    c.close()


def test_reconcile_clean_then_detects_tamper(data_dir):
    c = dualwrite.connect_dual()
    rep = rr.report(c)
    # 种子的过期锁在首次 sweep 前：直接对账时 ghost 锁无镜像 → 有差异
    c.close()

    from app import main
    from datetime import datetime, timezone
    T = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    c = dualwrite.connect_dual(); main.sweep(c, T); c.commit()

    # 新建一个干净愿望并 claim/release 后对账无差异
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean")).lastrowid
    _claim(c, wid, "alice", T); _release(c, wid, T)
    c.commit()
    rep = rr.report(c)
    kinds = {(d["wish_id"], d["kind"]) for d in rep["diffs"]}
    assert (wid, "missing_mirror") not in kinds
    assert all(d["wish_id"] != wid for d in rep["diffs"])
    assert rep["chain"]["ok"]

    # 篡改主库状态 → status_mismatch
    c.execute("UPDATE wishes SET status='claimed', claimer='mallory' WHERE id=?", (wid,))
    c.commit()
    rep = rr.report(c)
    wd = [d for d in rep["diffs"] if d["wish_id"] == wid]
    assert {d["kind"] for d in wd} == {"status_mismatch", "claimer_mismatch"}
    assert rep["ok"] is False

    # 镜像侧显示「最近镜像」仍为 release/released
    mm = next(d for d in wd if d["kind"] == "status_mismatch")
    assert mm["mirror"]["status"] == "released" and mm["primary"]["status"] == "claimed"
    c.close()


def test_reconcile_detects_missing_and_phantom(data_dir):
    from datetime import datetime, timezone
    T = datetime(2026, 3, 1, tzinfo=timezone.utc)
    c = dualwrite.connect_dual()
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean")).lastrowid
    _claim(c, wid, "alice", T); c.commit()

    # 主库多出无镜像愿望
    ghost = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                      ("g", "", "open", "clean")).lastrowid
    # 主库删除已有镜像的愿望
    c.execute("DELETE FROM wishes WHERE id=?", (wid,)); c.commit()
    rep = rr.report(c)
    kinds = {(d["wish_id"], d["kind"]) for d in rep["diffs"]}
    assert (ghost, "missing_mirror") in kinds
    assert (wid, "primary_missing") in kinds
    c.close()


def test_force_rebuild_append_only_and_history(data_dir):
    from datetime import datetime, timezone
    T = datetime(2026, 3, 1, tzinfo=timezone.utc)
    c = dualwrite.connect_dual()
    wid = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    ("t", "", "open", "clean")).lastrowid
    c.commit()
    before_rows = len(sc.all_events(c))

    # 重建：每个愿望追加 rebuild_snapshot，差异清零，旧行一行不动
    out = rr.rebuild(c, "append_only", T.isoformat())
    assert out["rebuild"]["rows_appended"] == out["rebuild"]["wish_count"]
    assert out["after"]["diff_count"] == 0
    assert out["after"]["chain"]["ok"]
    events = sc.all_events(c)
    assert len(events) == before_rows + out["rebuild"]["wish_count"]
    assert all(e["action"] == "rebuild_snapshot"
               for e in events[before_rows:before_rows + out["rebuild"]["wish_count"]])

    # 重建履历与对账页同钉：report 自带履历
    hist = sc.rebuild_history(c)
    assert hist[-1]["mode"] == "append_only" and hist[-1]["result"] == "ok"
    assert rr.report(c)["rebuild_history"][-1]["rebuild_seq"] == hist[-1]["rebuild_seq"]

    # 再次重建继续追加，不覆盖上一批快照
    rr.rebuild(c, "append_only", T.isoformat())
    assert len(sc.all_events(c)) == len(events) + out["rebuild"]["wish_count"]
    c.close()


def test_force_rebuild_overwrite_rejected_and_logged(data_dir):
    from datetime import datetime, timezone
    c = dualwrite.connect_dual()
    before = len(sc.all_events(c))
    with pytest.raises(HTTPException) as ei:
        rr.rebuild(c, "overwrite", datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat())
    assert ei.value.status_code == 400
    # 镜像行零变化
    assert len(sc.all_events(c)) == before
    hist = sc.rebuild_history(c)
    assert hist[-1]["mode"] == "overwrite" and hist[-1]["result"] == "rejected"
    assert "forbidden" in hist[-1]["reason"]
    c.close()
