"""Sidecar mirror storage (append-only ledger).

独立于主库 wishes 的镜像仓：wish_mirror 表只追加，链状 sha256 校验和；
UPDATE/DELETE 由触发器在存储层硬禁止，任何重建都只能追加新行。
"""
import hashlib
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}wish_mirror(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  wish_id INTEGER NOT NULL,
  action TEXT NOT NULL,              -- claim | release | fulfill | rebuild_snapshot
  status TEXT NOT NULL,              -- 写入时主库状态
  claimer TEXT,
  occurred_at TEXT NOT NULL,
  prev_checksum TEXT NOT NULL,
  checksum TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS {p}wish_mirror_no_update
BEFORE UPDATE ON {p}wish_mirror
BEGIN SELECT RAISE(ABORT, 'mirror_immutable: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS {p}wish_mirror_no_delete
BEFORE DELETE ON {p}wish_mirror
BEGIN SELECT RAISE(ABORT, 'mirror_immutable: delete forbidden'); END;
CREATE TABLE IF NOT EXISTS {p}sidecar_rebuilds(
  rebuild_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,                -- append_only | overwrite
  result TEXT NOT NULL,              -- ok | rejected
  reason TEXT,
  wish_count INTEGER NOT NULL DEFAULT 0,
  rows_appended INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

IMMUTABLE_MSG = "mirror_immutable"
GENESIS = "0" * 64


def sidecar_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent.parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "sidecar.db"


def _init(c: sqlite3.Connection, prefix: str):
    c.executescript(SCHEMA.format(p=prefix))
    c.commit()


def connect():
    c = sqlite3.connect(sidecar_path())
    c.row_factory = sqlite3.Row
    _init(c, "")
    return c


def compute_checksum(prev_checksum: str, wish_id: int, action: str,
                     status: str, claimer: str | None, occurred_at: str) -> str:
    payload = "|".join([
        prev_checksum, str(wish_id), action, status,
        claimer if claimer is not None else "", occurred_at,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_event(c: sqlite3.Connection, wish_id: int, action: str, status: str,
                 claimer: str | None, occurred_at: str) -> dict:
    """追加一行镜像。可作用于独立连接或以 sidecar 名 ATTACH 的主连接。"""
    q = lambda sql: sql.replace("wish_mirror", "sidecar.wish_mirror") if _attached(c) else sql
    row = c.execute(q("SELECT seq, checksum FROM wish_mirror ORDER BY seq DESC LIMIT 1")).fetchone()
    prev = row["checksum"] if row else GENESIS
    checksum = compute_checksum(prev, wish_id, action, status, claimer, occurred_at)
    cur = c.execute(
        q("INSERT INTO wish_mirror(wish_id,action,status,claimer,occurred_at,prev_checksum,checksum)"
          " VALUES (?,?,?,?,?,?,?)"),
        (wish_id, action, status, claimer, occurred_at, prev, checksum),
    )
    return {
        "seq": cur.lastrowid, "wish_id": wish_id, "action": action,
        "status": status, "claimer": claimer, "occurred_at": occurred_at,
        "prev_checksum": prev, "checksum": checksum,
    }


def _attached(c: sqlite3.Connection) -> bool:
    return any(r[1] == "sidecar" for r in c.execute("PRAGMA database_list"))


def attach(c: sqlite3.Connection) -> sqlite3.Connection:
    """把 sidecar 以 sidecar 名挂到主库连接，使双写可在同一事务内提交。"""
    if not _attached(c):
        c.execute("ATTACH DATABASE ? AS sidecar", (str(sidecar_path()),))
        _init(c, "sidecar.")
    return c


def latest_per_wish(c: sqlite3.Connection) -> dict[int, dict]:
    q = ("SELECT * FROM sidecar.wish_mirror WHERE seq IN "
         "(SELECT MAX(seq) FROM sidecar.wish_mirror GROUP BY wish_id)") \
        if _attached(c) else \
        ("SELECT * FROM wish_mirror WHERE seq IN "
         "(SELECT MAX(seq) FROM wish_mirror GROUP BY wish_id)")
    return {r["wish_id"]: dict(r) for r in c.execute(q)}


def all_events(c: sqlite3.Connection) -> list[dict]:
    q = "SELECT * FROM sidecar.wish_mirror ORDER BY seq" if _attached(c) \
        else "SELECT * FROM wish_mirror ORDER BY seq"
    return [dict(r) for r in c.execute(q)]


def verify_chain(c: sqlite3.Connection) -> dict:
    """从 genesis 重算全链，返回链状态与尾校验和。"""
    prev = GENESIS
    count = 0
    for e in all_events(c):
        expected = compute_checksum(prev, e["wish_id"], e["action"], e["status"],
                                    e["claimer"], e["occurred_at"])
        if e["prev_checksum"] != prev or e["checksum"] != expected:
            return {"ok": False, "events": count, "broken_seq": e["seq"],
                    "expected": expected, "actual": e["checksum"], "tail_checksum": None}
        prev = e["checksum"]
        count += 1
    return {"ok": True, "events": count, "broken_seq": None,
            "expected": None, "actual": None, "tail_checksum": prev if count else GENESIS}


def record_rebuild(c: sqlite3.Connection, mode: str, result: str, reason: str | None,
                   wish_count: int, rows_appended: int, created_at: str) -> dict:
    q = lambda sql: sql.replace("sidecar_rebuilds", "sidecar.sidecar_rebuilds") if _attached(c) else sql
    cur = c.execute(
        q("INSERT INTO sidecar_rebuilds(mode,result,reason,wish_count,rows_appended,created_at)"
          " VALUES (?,?,?,?,?,?)"),
        (mode, result, reason, wish_count, rows_appended, created_at),
    )
    return {"rebuild_seq": cur.lastrowid, "mode": mode, "result": result, "reason": reason,
            "wish_count": wish_count, "rows_appended": rows_appended, "created_at": created_at}


def rebuild_history(c: sqlite3.Connection) -> list[dict]:
    q = "SELECT * FROM sidecar.sidecar_rebuilds ORDER BY rebuild_seq" if _attached(c) \
        else "SELECT * FROM sidecar_rebuilds ORDER BY rebuild_seq"
    return [dict(r) for r in c.execute(q)]
