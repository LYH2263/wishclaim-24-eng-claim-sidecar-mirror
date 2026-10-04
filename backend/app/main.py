from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.sidecar import store as sidecar_store
from app.sidecar import dualwrite, reconcile


def _init_sidecar():
    s = sidecar_store.connect()
    sidecar_store.init_db(s)
    s.close()


app = FastAPI(title="Wishclaim", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup():
    seed.init_db()
    _init_sidecar()


def now(): return datetime.now(timezone.utc)


def _pair():
    """主库 + sidecar 连接对。"""
    return connect(), sidecar_store.connect()


def _close(c, s):
    c.close(); s.close()


def ttl():
    c = connect(); row = c.execute("SELECT value FROM settings WHERE key='ttl_seconds'").fetchone(); c.close()
    return int(row["value"] if row else 86400)


def sweep(c, s):
    """TTL 自动释放也走双写适配（镜像 ttl_release 行）。"""
    ids = dualwrite.sweep_expired(c, s, now())
    if ids:
        c.commit()


@app.get("/api/health")
def health(): return {"ok": True, "project": "wishclaim"}

@app.get("/api/wishes")
def list_wishes():
    c, s = _pair(); sweep(c, s)
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes ORDER BY id DESC")]
    _close(c, s); return rows

@app.get("/api/wishes/{wid}")
def get_wish(wid: int):
    c, s = _pair(); sweep(c, s)
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    _close(c, s)
    if not r: raise HTTPException(404, "not found")
    return dict(r)

class WishIn(BaseModel):
    title: str
    note: str = ""

@app.post("/api/wishes")
def create_wish(body: WishIn):
    c = connect()
    cur = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.note, "open", "clean"))
    c.commit(); wid = cur.lastrowid; c.close(); return {"id": wid}

class ClaimIn(BaseModel):
    claimer: str

@app.post("/api/wishes/{wid}/claim")
def claim(wid: int, body: ClaimIn):
    c, s = _pair(); sweep(c, s)
    try:
        res = dualwrite.apply_claim(c, s, wid, body.claimer, now(), ttl())
    except dualwrite.NotFound:
        _close(c, s); raise HTTPException(404, "not found")
    except dualwrite.Conflict as e:
        _close(c, s); raise HTTPException(409, e.reason)
    _close(c, s)
    return res["lock"]

@app.post("/api/wishes/{wid}/release")
def release(wid: int):
    c, s = _pair()
    try:
        dualwrite.apply_release(c, s, wid, now())
    except dualwrite.NotFound:
        _close(c, s); raise HTTPException(404, "not found")
    except dualwrite.BadState as e:
        _close(c, s); raise HTTPException(400, e.reason)
    _close(c, s); return {"ok": True, "status": "released"}

@app.post("/api/wishes/{wid}/fulfill")
def fulfill(wid: int):
    c, s = _pair()
    try:
        dualwrite.apply_fulfill(c, s, wid, now())
    except dualwrite.NotFound:
        _close(c, s); raise HTTPException(404, "not found")
    except dualwrite.BadState as e:
        _close(c, s); raise HTTPException(400, e.reason)
    _close(c, s); return {"ok": True, "status": "fulfilled"}

@app.get("/api/mine")
def mine(claimer: str):
    c, s = _pair(); sweep(c, s)
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE claimer=?", (claimer,))]
    _close(c, s); return rows

@app.get("/api/done")
def done():
    c, s = _pair(); sweep(c, s)
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]
    _close(c, s); return rows

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

@app.get("/api/rules")
def rules():
    return {
        "mutex": "同一愿望同时只能被一人认领",
        "ttl": "认领超时未核销则自动释放",
        "fulfill": "核销后状态变为 fulfilled",
        "sidecar": "每次 claim/release 追加只读写镜像行（哈希链），主库改写不动镜像历史",
        "rebuild": "force 重建只追加 rebuild 修正快照，不覆盖最新快照，履历与对账同钉",
    }

# ---- 对账入口：主库当前态 vs 最近镜像 + 哈希链 + 重建履历（同钉） ----

class RebuildIn(BaseModel):
    reason: str | None = None

@app.get("/api/audit/reconcile")
def audit_reconcile():
    c, s = _pair(); sweep(c, s)
    report = reconcile.build_report(c, s)
    _close(c, s); return report

@app.post("/api/audit/rebuild")
def audit_rebuild(body: RebuildIn | None = None):
    c, s = _pair()
    report = reconcile.force_rebuild(c, s, reason=body.reason if body else None)
    _close(c, s); return report
