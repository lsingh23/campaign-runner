"""Campaign Runner web app.  Run:  .venv/bin/uvicorn app:app --port 8000"""
import csv
import io
import json
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import checks
import conductor
import db
import llm

ROOT = Path(__file__).parent
SEED = json.loads((ROOT / "seed" / "harbourline.json").read_text())


@asynccontextmanager
async def lifespan(_):
    db.init()
    conductor.resume_all()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
T = Jinja2Templates(directory=ROOT / "templates")
T.env.globals.update(TYPE_LABELS=conductor.TYPE_LABELS, STEPS=conductor.STEPS, MODE=llm.MODE,
                     MODELS={"research": llm.RESEARCH_MODEL, "worker": llm.WORKER_MODEL,
                             "writer": llm.WRITER_MODEL, "reviewer": llm.REVIEWER_MODEL})
T.env.filters["md"] = lambda s: markdown.markdown(s or "", extensions=["tables"])
T.env.filters["words"] = checks.word_count
T.env.filters["clock"] = lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))


def get(cid):
    c = db.campaign(cid)
    if not c:
        raise HTTPException(404)
    return c


def page(request, name, **ctx):
    return T.TemplateResponse(request, name, ctx)


# What the overview shows: the plan's simple chain. Each stage groups the conductor's internal states,
# and `hum` marks the three stages where a person steps in.
STAGES = [
    ("Brief", "one form", ["BRIEF"], None, False),
    ("Research", "how AI answers today", ["RESEARCH"], "research", False),
    ("Audit", "the audit sheet", ["AUDIT"], "audit", False),
    ("Client approves", "ticks Yes / No", ["AWAIT_CLIENT"], "client", True),
    ("Plan", "you OK the titles", ["PLAN", "AWAIT_OPERATOR"], "plan", True),
    ("Write + check", "you read the first 2", ["WRITE_FIRST", "AWAIT_FIRST", "WRITE", "FINAL_CHECK"], "articles", True),
    ("Ready", "download", ["DONE"], None, False),
]

# The banner while a step runs: what is happening, and what it replaces today.
NOW = {
    "RESEARCH": ("Researching how AI answers the query",
                 "Today: the dissector skill in Claude + Chrome, then a PDF download."),
    "AUDIT": ("Building the audit sheet from the research",
              "Today: uploading the PDFs to ChatGPT's LLM Visibility Audit."),
    "PLAN": ("Planning the articles from the client's Yes answers",
             "Today: Topical Authority Builder with a long custom prompt."),
    "WRITE_FIRST": ("Writing the first 2 articles",
                    "One fresh writer per article, then rule checks and a separate reviewer. Today: “next 4 articles” in one chat."),
    "WRITE": ("Writing the rest, with your notes",
              "One fresh writer per article, then rule checks and a separate reviewer."),
    "FINAL_CHECK": ("Checking the whole set for repeats",
                    "Today: checked by hand against earlier articles."),
}


def step_status(c):
    cur = next((i for i, s in enumerate(STAGES) if c["state"] in s[2]), 0)
    out = []
    for i, (label, sub, states, link, hum) in enumerate(STAGES):
        if c["state"] == "DONE" or i < cur:
            st = "done"
        elif i == cur:
            st = "error" if c["error"] else ("human" if c["state"] in conductor.HUMAN else "active")
        else:
            st = "todo"
        href = None
        if link and st != "todo":
            href = f"/client/{c['client_token']}" if link == "client" else f"/c/{c['id']}/{link}"
        out.append({"label": label, "sub": sub, "status": st, "n": i + 1, "href": href, "hum": hum})
    return out


# ---------------------------------------------------------------- campaigns

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, "home.html", campaigns=db.q("SELECT * FROM campaigns ORDER BY id DESC"))


@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    return page(request, "new.html", s=SEED, f=SEED["client_facts"])


@app.post("/new")
async def new_campaign(request: Request):
    f = await request.form()
    lines = lambda k: [x.strip() for x in f.get(k, "").splitlines() if x.strip()]
    brief = {
        "client_name": f["client_name"].strip(), "client_url": f["client_url"].strip(),
        "location": f["location"].strip(), "target_query": f["target_query"].strip(),
        "site_type": f["site_type"], "runs": int(f["runs"]),
        "mix": {t: int(f.get(f"mix_{t}") or 0) for t in conductor.TYPE_LABELS},
        "words_min": int(f["words_min"]), "words_max": int(f["words_max"]),
        "sources_required": int(f["sources_required"]),
        "exclude_titles": lines("exclude_titles"),
        "client_facts": {"about": f["about"].strip(), "services": lines("services"),
                         "credentials": lines("credentials"), "pricing": f["pricing"].strip(),
                         "not_offered": [x.strip() for x in f["not_offered"].split(",") if x.strip()],
                         "first_hand_notes": f["first_hand_notes"].strip()},
    }
    cid = db.create_campaign(brief)
    db.event(cid, "BRIEF", f"Campaign created: {sum(brief['mix'].values())} articles, {brief['site_type'].replace('_', ' ')}, "
             f"{brief['runs']} research runs", "ok")
    conductor.start(cid)
    return RedirectResponse(f"/c/{cid}", 303)


@app.get("/c/{cid}", response_class=HTMLResponse)
def campaign_view(request: Request, cid: int):
    return page(request, "campaign.html", c=get(cid))


@app.get("/c/{cid}/live", response_class=HTMLResponse)
def campaign_live(request: Request, cid: int):
    c = get(cid)
    arts = db.articles(cid)
    return page(request, "_live.html", c=c, steps=step_status(c), now=NOW.get(c["state"]), events=db.events(cid, 60), arts=arts,
                counts=Counter(a["status"] for a in arts), n_plan=len(db.plan_items(cid)),
                fc=db.artifact(cid, "final_check") or {})


@app.post("/c/{cid}/retry")
def retry(cid: int):
    c = get(cid)
    db.set_error(cid, None)
    db.event(cid, c["state"], "Retrying step")
    conductor.start(cid)
    return RedirectResponse(f"/c/{cid}", 303)


# ---------------------------------------------------------------- research + audit

@app.get("/c/{cid}/research", response_class=HTMLResponse)
def research_view(request: Request, cid: int):
    c = get(cid)
    d = db.artifact(cid, "dissection") or {}
    hosts = conductor.host_counts(cid)
    return page(request, "research.html", c=c, runs=db.research_runs(cid), d=d, fanouts=conductor.fanout_index(cid),
                hosts=hosts.most_common(25), host_types=d.get("host_types", {}))


@app.get("/c/{cid}/audit", response_class=HTMLResponse)
def audit_view(request: Request, cid: int):
    c = get(cid)
    return page(request, "audit.html", c=c, rows=db.audit_rows(cid), a=db.artifact(cid, "audit") or {},
                d=db.artifact(cid, "dissection") or {}, fanouts={f["id"]: f["query"] for f in conductor.fanout_index(cid)},
                hosts=conductor.host_counts(cid).most_common(40))


@app.get("/c/{cid}/audit.csv")
def audit_csv(cid: int):
    c = get(cid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group", "keyword", "context", "evidence", "supported?", "client notes"])
    fan = {f["id"]: f["query"] for f in conductor.fanout_index(cid)}
    for r in db.audit_rows(cid):
        w.writerow([r["grp"], r["keyword"], r["context"], " | ".join(fan.get(e, e) for e in r["evidence"]),
                    r["supported"] or "", r["notes"]])
    name = f"llm-visibility-audit-{c['id']}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------- client check-in

@app.get("/client/{token}", response_class=HTMLResponse)
def client_view(request: Request, token: str):
    c = db.campaign_by_token(token)
    if not c:
        raise HTTPException(404)
    groups = defaultdict(list)
    for r in db.audit_rows(c["id"]):
        groups[r["grp"]].append(r)
    return page(request, "client.html", c=c, groups=groups, token=token)


@app.post("/client/{token}")
async def client_save(request: Request, token: str):
    c = db.campaign_by_token(token)
    if not c or c["state"] != "AWAIT_CLIENT":
        raise HTTPException(409, "This sheet is no longer open for answers")
    f = await request.form()
    action = f.get("action")
    if action == "fill":
        conductor.fill_from_profile(c["id"], c["brief"])
        return RedirectResponse(f"/client/{token}", 303)
    for r in db.audit_rows(c["id"]):
        db.x("UPDATE audit_rows SET supported=?, notes=? WHERE id=?",
             f.get(f"s_{r['id']}") or None, f.get(f"n_{r['id']}", ""), r["id"])
    if action == "submit":
        rows = db.audit_rows(c["id"])
        if any(not r["supported"] for r in rows):
            return RedirectResponse(f"/client/{token}?missing=1", 303)
        yes = sum(r["supported"] == "Yes" for r in rows)
        db.event(c["id"], "AWAIT_CLIENT", f"Client answered: {yes} Yes, {len(rows) - yes} No ✓", "ok")
        db.set_state(c["id"], "PLAN")
        conductor.start(c["id"])
        return RedirectResponse(f"/client/{token}?sent=1", 303)
    return RedirectResponse(f"/client/{token}?saved=1", 303)


# ---------------------------------------------------------------- operator check-in

@app.get("/c/{cid}/plan", response_class=HTMLResponse)
def plan_view(request: Request, cid: int):
    c = get(cid)
    items = db.plan_items(cid)
    errs = conductor.validate_plan([i["brief"] | {"title": i["title"]} for i in items], c["brief"],
                                   {f["id"] for f in conductor.fanout_index(cid)}) if items else []
    groups = defaultdict(list)
    for i in items:
        groups[i["type"]].append(i)
    return page(request, "plan.html", c=c, groups=groups, errs=errs,
                fanouts={f["id"]: f["query"] for f in conductor.fanout_index(cid)})


@app.post("/c/{cid}/plan")
async def plan_approve(request: Request, cid: int):
    c = get(cid)
    if c["state"] != "AWAIT_OPERATOR":
        raise HTTPException(409, "The plan is not waiting for approval")
    f = await request.form()
    kept = edited = 0
    for i in db.plan_items(cid):
        title = (f.get(f"t_{i['id']}") or i["title"]).strip()
        keep = f.get(f"k_{i['id']}") == "on"
        edited += title != i["title"]
        kept += keep
        db.x("UPDATE plan_items SET title=?, status=? WHERE id=?", title, "approved" if keep else "removed", i["id"])
    db.event(cid, "AWAIT_OPERATOR", f"Operator approved {kept} titles ({edited} edited) ✓", "ok")
    db.set_state(cid, "WRITE_FIRST")
    conductor.start(cid)
    return RedirectResponse(f"/c/{cid}", 303)


# ---------------------------------------------------------------- articles

@app.get("/c/{cid}/articles", response_class=HTMLResponse)
def articles_view(request: Request, cid: int):
    c = get(cid)
    fc = db.artifact(cid, "final_check") or {}
    return page(request, "articles.html", c=c, arts=db.articles(cid), fc=fc)


@app.get("/c/{cid}/articles/{aid}", response_class=HTMLResponse)
def article_view(request: Request, cid: int, aid: int):
    c = get(cid)
    a = db.article(aid)
    if not a or a["campaign_id"] != cid:
        raise HTTPException(404)
    fc = db.artifact(cid, "final_check") or {}
    issues = [i for i in fc.get("issues", []) if aid in (i["a"], i["b"])]
    titles = {x["id"]: x["plan_title"] for x in db.articles(cid)}
    return page(request, "article.html", c=c, a=a, issues=issues, titles=titles,
                fanouts={f["id"]: f["query"] for f in conductor.fanout_index(cid)})


@app.post("/c/{cid}/articles/{aid}/accept")
def article_accept(cid: int, aid: int):
    get(cid)
    db.update_article(aid, status="accepted")
    db.event(cid, "WRITE", f"Operator accepted flagged article #{aid} as is", "ok")
    return RedirectResponse(f"/c/{cid}/articles/{aid}", 303)


@app.post("/c/{cid}/first")
def first_approve(cid: int, notes: str = Form("")):
    c = get(cid)
    if c["state"] != "AWAIT_FIRST":
        raise HTTPException(409, "The first articles are not waiting for approval")
    notes = notes.strip()
    db.put_artifact(cid, "first_review", {"notes": notes})
    db.event(cid, "AWAIT_FIRST", "Operator approved the first articles"
             + (f" with notes for the rest: “{notes[:160]}”" if notes else ", no notes") + " ✓", "ok")
    db.set_state(cid, "WRITE")
    conductor.start(cid)
    return RedirectResponse(f"/c/{cid}", 303)


@app.get("/c/{cid}/export.zip")
def export(cid: int):
    c = get(cid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for a in db.articles(cid):
            if not a["final"]:
                continue
            art = a["final"]
            slug = "".join(ch if ch.isalnum() else "-" for ch in art["title"].lower()).strip("-")[:70]
            src = "\n".join(f"- [{s.get('title') or s['url']}]({s['url']})" for s in art["sources"])
            z.writestr(f"{a['plan_idx'] + 1:02d}-{slug}.md",
                       f"---\ntitle: \"{art['title']}\"\nmeta_description: \"{art['meta_description']}\"\n"
                       f"type: {a['plan_type']}\nstatus: {a['status']}\n---\n\n# {art['title']}\n\n"
                       f"{art['body_markdown']}\n\n## Sources\n{src}\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=campaign-{c['id']}-articles.zip"})
