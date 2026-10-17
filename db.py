"""The campaign record: one SQLite file, saved after every step, so a run can wait days
for the client or survive a restart and pick up where it stopped."""
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("CAMPAIGN_DB", Path(__file__).parent / "data" / "campaigns.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY, name TEXT, brief TEXT, state TEXT, client_token TEXT,
  cost_usd REAL DEFAULT 0, error TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS research_runs (
  id INTEGER PRIMARY KEY, campaign_id INTEGER, run_idx INTEGER, data TEXT,
  UNIQUE (campaign_id, run_idx));
CREATE TABLE IF NOT EXISTS artifacts (
  campaign_id INTEGER, kind TEXT, data TEXT, PRIMARY KEY (campaign_id, kind));
CREATE TABLE IF NOT EXISTS audit_rows (
  id INTEGER PRIMARY KEY, campaign_id INTEGER, grp TEXT, keyword TEXT, context TEXT,
  evidence TEXT, supported TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS plan_items (
  id INTEGER PRIMARY KEY, campaign_id INTEGER, idx INTEGER, type TEXT, title TEXT,
  brief TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY, campaign_id INTEGER, plan_item_id INTEGER UNIQUE, status TEXT,
  attempts TEXT DEFAULT '[]', final TEXT, cost_usd REAL DEFAULT 0, seconds REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, campaign_id INTEGER, ts REAL, step TEXT, kind TEXT, message TEXT);
"""

JSON_COLS = {"brief", "data", "evidence", "attempts", "final"}


@contextmanager
def _conn():
    """Commit (or roll back) and always close: sqlite3's own `with` only commits, so the
    polling overview would otherwise leak a file handle per request until the OS limit."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


def q(sql, *args):
    with _lock, _conn() as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def x(sql, *args):
    with _lock, _conn() as c:
        return c.execute(sql, args).lastrowid


def dump(v):
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- campaigns

def create_campaign(brief: dict) -> int:
    now = time.time()
    return x("INSERT INTO campaigns (name, brief, state, client_token, created_at, updated_at) VALUES (?,?,?,?,?,?)",
             f"{brief['client_name']} · {brief['target_query']}", dump(brief), "RESEARCH",
             secrets.token_urlsafe(8), now, now)


def campaign(cid):
    return one("SELECT * FROM campaigns WHERE id=?", cid)


def campaign_by_token(token):
    return one("SELECT * FROM campaigns WHERE client_token=?", token)


def set_state(cid, state):
    x("UPDATE campaigns SET state=?, updated_at=? WHERE id=?", state, time.time(), cid)


def set_error(cid, error):
    x("UPDATE campaigns SET error=?, updated_at=? WHERE id=?", error, time.time(), cid)


def add_cost(cid, usd):
    if usd:
        x("UPDATE campaigns SET cost_usd = cost_usd + ? WHERE id=?", usd, cid)


def event(cid, step, message, kind="info"):
    x("INSERT INTO events (campaign_id, ts, step, kind, message) VALUES (?,?,?,?,?)", cid, time.time(), step, kind, message)


def events(cid, limit=200):
    return q("SELECT * FROM events WHERE campaign_id=? ORDER BY id DESC LIMIT ?", cid, limit)


# ---------------------------------------------------------------- artifacts

def put_artifact(cid, kind, data):
    x("INSERT OR REPLACE INTO artifacts (campaign_id, kind, data) VALUES (?,?,?)", cid, kind, dump(data))


def artifact(cid, kind):
    r = one("SELECT data FROM artifacts WHERE campaign_id=? AND kind=?", cid, kind)
    return r["data"] if r else None


def research_runs(cid):
    return [r["data"] for r in q("SELECT data FROM research_runs WHERE campaign_id=? ORDER BY run_idx", cid)]


def save_research_run(cid, idx, data):
    x("INSERT OR REPLACE INTO research_runs (campaign_id, run_idx, data) VALUES (?,?,?)", cid, idx, dump(data))


def audit_rows(cid):
    return q("SELECT * FROM audit_rows WHERE campaign_id=? ORDER BY id", cid)


def plan_items(cid, status=None):
    if status:
        return q("SELECT * FROM plan_items WHERE campaign_id=? AND status=? ORDER BY idx", cid, status)
    return q("SELECT * FROM plan_items WHERE campaign_id=? ORDER BY idx", cid)


def articles(cid):
    return q("""SELECT a.*, p.title AS plan_title, p.type AS plan_type, p.idx AS plan_idx FROM articles a
                JOIN plan_items p ON p.id = a.plan_item_id WHERE a.campaign_id=? ORDER BY p.idx""", cid)


def article(aid):
    return one("""SELECT a.*, p.title AS plan_title, p.type AS plan_type, p.brief AS brief FROM articles a
                  JOIN plan_items p ON p.id = a.plan_item_id WHERE a.id=?""", aid)


def update_article(aid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = [dump(v) if k in JSON_COLS else v for k, v in fields.items()]
    x(f"UPDATE articles SET {sets} WHERE id=?", *vals, aid)
