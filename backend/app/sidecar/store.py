"""Sidecar mirror store (append-only) for wish claim/release events.

政策（拍板，对账页同钉）:
- 镜像行只追加 (append-only)，永不 UPDATE/DELETE 历史行；
- 每行携带 sha256 校验和，并以前一行校验和为前导形成哈希链；
- force 重建同样只追加 action='rebuild' 的修正快照行，不覆盖任何旧行；
- 重建履历写入 sidecar_rebuilds，与对账报告同一入口返回。
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

GENESIS = "0" * 64
ACTIONS = ("claim", "release", "ttl_release", "fulfill", "rebuild")


def sidecar_path() -> Path:
    d = Path(os.environ.get("SIDECAR_DIR")
             or os.environ.get("DATA_DIR")
             or Path(__file__).resolve().parent.parent / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d / "wishclaim_sidecar.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or sidecar_path())
    c.row_factory = sqlite3.Row
    return c


def init_db(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sidecar_events(
      seq INTEGER PRIMARY KEY,
      wish_id INTEGER NOT NULL,
      action TEXT NOT NULL,
      claimer TEXT,
      ts TEXT NOT NULL,
      status TEXT,
      prev_hash TEXT NOT NULL,
      checksum TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sidecar_wish ON sidecar_events(wish_id, seq);
    CREATE TABLE IF NOT EXISTS sidecar_rebuilds(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      reason TEXT,
      mode TEXT NOT NULL,
      base_seq INTEGER NOT NULL,
      events_appended INTEGER NOT NULL,
      drift_before INTEGER NOT NULL,
      chain_checksum TEXT NOT NULL,
      details TEXT
    );
    """)
    c.commit()


def _canonical(seq: int, wish_id: int, action: str, claimer: str | None,
               ts: str, status: str | None, prev_hash: str) -> str:
    return "|".join([str(seq), str(wish_id), action, claimer or "",
                     ts, status or "", prev_hash])


def compute_checksum(seq: int, wish_id: int, action: str, claimer: str | None,
                     ts: str, status: str | None, prev_hash: str) -> str:
    return hashlib.sha256(_canonical(seq, wish_id, action, claimer, ts,
                                     status, prev_hash).encode("utf-8")).hexdigest()


def _row_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def append_event(c: sqlite3.Connection, wish_id: int, action: str, ts: str,
                 *, claimer: str | None = None, status: str | None = None,
                 commit: bool = True) -> dict:
    """追加一条镜像行。seq 与校验和由链尾确定性推出。"""
    if action not in ACTIONS:
        raise ValueError(f"bad action: {action}")
    tip = c.execute(
        "SELECT seq, checksum FROM sidecar_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq = (tip["seq"] + 1) if tip else 1
    prev_hash = tip["checksum"] if tip else GENESIS
    checksum = compute_checksum(seq, wish_id, action, claimer, ts, status, prev_hash)
    c.execute(
        "INSERT INTO sidecar_events(seq,wish_id,action,claimer,ts,status,prev_hash,checksum)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (seq, wish_id, action, claimer, ts, status, prev_hash, checksum),
    )
    if commit:
        c.commit()
    return _row_dict(c.execute(
        "SELECT * FROM sidecar_events WHERE seq=?", (seq,)).fetchone())


def latest_event(c: sqlite3.Connection, wish_id: int) -> dict | None:
    r = c.execute(
        "SELECT * FROM sidecar_events WHERE wish_id=? ORDER BY seq DESC LIMIT 1",
        (wish_id,),
    ).fetchone()
    return _row_dict(r) if r else None


def events_for(c: sqlite3.Connection, wish_id: int) -> list[dict]:
    return [_row_dict(r) for r in c.execute(
        "SELECT * FROM sidecar_events WHERE wish_id=? ORDER BY seq", (wish_id,))]


def mirrored_wish_ids(c: sqlite3.Connection) -> set[int]:
    return {r["wish_id"] for r in c.execute(
        "SELECT DISTINCT wish_id FROM sidecar_events")}


def verify_chain(c: sqlite3.Connection) -> dict:
    """全链重算校验和。任何内容/链接被改写都会在 breaks 中暴露。"""
    rows = c.execute("SELECT * FROM sidecar_events ORDER BY seq").fetchall()
    breaks = []
    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            breaks.append({"seq": r["seq"], "reason": "prev_hash_mismatch",
                           "expected_prev": prev, "stored_prev": r["prev_hash"]})
        recomputed = compute_checksum(
            r["seq"], r["wish_id"], r["action"], r["claimer"],
            r["ts"], r["status"], r["prev_hash"])
        if recomputed != r["checksum"]:
            breaks.append({"seq": r["seq"], "reason": "checksum_mismatch",
                           "expected": recomputed, "stored": r["checksum"]})
        prev = r["checksum"]
    return {"ok": not breaks, "length": len(rows),
            "tip_seq": rows[-1]["seq"] if rows else 0,
            "tip_checksum": prev if rows else GENESIS, "breaks": breaks}


def log_rebuild(c: sqlite3.Connection, *, ts: str, reason: str | None,
                base_seq: int, events_appended: int, drift_before: int,
                chain_checksum: str, details: list[dict], commit: bool = True) -> int:
    cur = c.execute(
        "INSERT INTO sidecar_rebuilds(ts,reason,mode,base_seq,events_appended,"
        "drift_before,chain_checksum,details) VALUES (?,?,?,?,?,?,?,?)",
        (ts, reason, "append", base_seq, events_appended, drift_before,
         chain_checksum, json.dumps(details, ensure_ascii=False)),
    )
    if commit:
        c.commit()
    return cur.lastrowid


def list_rebuilds(c: sqlite3.Connection) -> list[dict]:
    import json
    out = []
    for r in c.execute("SELECT * FROM sidecar_rebuilds ORDER BY id"):
        d = _row_dict(r)
        d["details"] = json.loads(d["details"] or "[]")
        out.append(d)
    return out
