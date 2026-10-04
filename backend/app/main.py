from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.claim_lock import claim_allowed, lock_payload, release_if_expired
from app.modules import dualwrite
from app.modules import reconcile_report

app = FastAPI(title="Wishclaim", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

def now(): return datetime.now(timezone.utc)

def ttl(c=None):
    own = c is None
    if own: c = connect()
    row = c.execute("SELECT value FROM settings WHERE key='ttl_seconds'").fetchone()
    if own: c.close()
    return int(row["value"] if row else 86400)

def sweep(c, ts=None):
    """TTL 自动释放同样是成功 release：主库更新与镜像追加同事务。"""
    ts = ts or now()
    for r in c.execute("SELECT * FROM wishes WHERE status='claimed'"):
        rel = release_if_expired(r["status"], r["expires_at"], ts)
        if rel:
            dualwrite.apply_release(c, r["id"], status=rel["status"],
                                    action="release", occurred_at=ts.isoformat())

@app.get("/api/health")
def health(): return {"ok": True, "project": "wishclaim"}

@app.get("/api/wishes")
def list_wishes():
    c = dualwrite.connect_dual(); sweep(c, now()); c.commit()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes ORDER BY id DESC")]; c.close(); return rows

@app.get("/api/wishes/{wid}")
def get_wish(wid: int):
    c = dualwrite.connect_dual(); sweep(c, now()); c.commit()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone(); c.close()
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
    ts = now()
    c = dualwrite.connect_dual(); sweep(c, ts)
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    allowed = claim_allowed(r["status"], r["claimer"], ts, r["expires_at"])
    if not allowed["ok"]:
        c.close(); raise HTTPException(409, allowed["reason"])
    p = lock_payload(body.claimer, ts, ttl(c))
    event = dualwrite.apply_claim(c, wid, p, occurred_at=p["claimed_at"])
    c.commit(); c.close()
    return {**p, "mirror_seq": event["seq"], "mirror_checksum": event["checksum"]}

@app.post("/api/wishes/{wid}/release")
def release(wid: int):
    c = dualwrite.connect_dual()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    if r["status"] != "claimed":
        c.close(); raise HTTPException(400, "not_claimed")
    event = dualwrite.apply_release(c, wid, status="released", action="release")
    c.commit(); c.close()
    return {"ok": True, "status": "released", "mirror_seq": event["seq"],
            "mirror_checksum": event["checksum"]}

@app.post("/api/wishes/{wid}/fulfill")
def fulfill(wid: int):
    c = dualwrite.connect_dual()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    if r["status"] != "claimed":
        c.close(); raise HTTPException(400, "need_claim")
    ts = now().isoformat()
    c.execute("UPDATE wishes SET status='fulfilled' WHERE id=?", (wid,))
    event = dualwrite.write_event(c, wid, "fulfill", "fulfilled", r["claimer"], ts)
    c.commit(); c.close()
    return {"ok": True, "status": "fulfilled", "mirror_seq": event["seq"],
            "mirror_checksum": event["checksum"]}

@app.get("/api/mine")
def mine(claimer: str):
    c = dualwrite.connect_dual(); sweep(c, now()); c.commit()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE claimer=?", (claimer,))]; c.close(); return rows

@app.get("/api/done")
def done():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]; c.close(); return rows

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

@app.get("/api/reconcile")
def reconcile():
    c = dualwrite.connect_dual()
    rep = reconcile_report.report(c); c.close(); return rep

class RebuildIn(BaseModel):
    # 拍板：仅 append_only；显式 overwrite 拒绝并记入重建履历
    mode: str = "append_only"

@app.post("/api/reconcile/rebuild")
def rebuild(body: RebuildIn):
    c = dualwrite.connect_dual()
    try:
        return reconcile_report.rebuild(c, body.mode, now().isoformat())
    finally:
        c.close()

@app.get("/api/rules")
def rules():
    return {
        "mutex": "同一愿望同时只能被一人认领",
        "ttl": "认领超时未核销则自动释放",
        "fulfill": "核销后状态变为 fulfilled",
        "sidecar": "每次成功 claim/release 同事务追加镜像行，历史镜像只追加不改写",
        "rebuild": "force 重建仅追加快照；overwrite 永久禁止并留痕",
    }
