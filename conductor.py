"""The conductor: runs the campaign's steps in order, checks each step's output before
moving on, pauses at the two human check-ins, and saves after every step."""
import re
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import checks
import db
import llm

STEPS = [
    ("BRIEF", "Brief", "one form"),
    ("RESEARCH", "Research", "every run, web search on"),
    ("AUDIT", "Audit", "auto-built sheet"),
    ("AWAIT_CLIENT", "Client approves", "ticks Yes / No"),
    ("PLAN", "Plan", "one brief per article"),
    ("AWAIT_OPERATOR", "Operator OKs titles", "edit, remove, approve"),
    ("WRITE_FIRST", "Write first 2", "fresh writer, rules, reviewer"),
    ("AWAIT_FIRST", "Operator OKs first 2", "approve, add notes"),
    ("WRITE", "Write the rest", "with the operator's notes"),
    ("FINAL_CHECK", "Final set check", "repeats across the set"),
    ("DONE", "Ready", "publish or send to writers"),
]
HUMAN = {"AWAIT_CLIENT", "AWAIT_OPERATOR", "AWAIT_FIRST"}
RUNNING = {"RESEARCH", "AUDIT", "PLAN", "WRITE_FIRST", "WRITE", "FINAL_CHECK"}
FIRST_N = 2          # articles the operator reads before the rest are written
WAITING_FOR = {"AWAIT_CLIENT": "the client's Yes/No answers", "AWAIT_OPERATOR": "the operator to approve the titles",
               "AWAIT_FIRST": "the operator to approve the first articles"}
TYPE_LABELS = {"comparison_3": "3-brand comparison", "comparison_4": "4-brand comparison",
               "buyers_guide": "Buyer's guide", "review_alternatives": "Review + alternatives",
               "listicle": "Listicle", "first_hand": "First-hand experience"}
MAX_RETRIES = 2
PARALLEL_WRITERS = 3

_threads: dict[int, threading.Thread] = {}


def start(cid: int):
    t = _threads.get(cid)
    if t and t.is_alive():
        return
    t = threading.Thread(target=_run, args=(cid,), daemon=True)
    _threads[cid] = t
    t.start()


def resume_all():
    """After a restart, pick every unfinished, non-waiting campaign back up from its record."""
    for c in db.q("SELECT id, state FROM campaigns WHERE error IS NULL"):
        if c["state"] in RUNNING:
            db.event(c["id"], c["state"], "Server restarted: resuming from the campaign record")
            start(c["id"])


def _run(cid):
    state = None
    try:
        while True:
            c = db.campaign(cid)
            state = c["state"]
            if state not in RUNNING or c["error"]:
                return
            nxt = HANDLERS[state](cid, c["brief"])
            db.set_state(cid, nxt)
            if nxt in HUMAN:
                db.event(cid, nxt, "Waiting for " + WAITING_FOR[nxt], "wait")
            if nxt == "DONE":
                db.event(cid, nxt, "Campaign complete", "ok")
    except Exception as e:
        traceback.print_exc()
        db.set_error(cid, f"{type(e).__name__}: {e}")
        db.event(cid, state, f"Stopped: {e}. Nothing before this step is lost; press Retry step.", "error")


# ---------------------------------------------------------------- shared views of the record

def fanout_index(cid):
    out, n = [], 0
    for r in db.research_runs(cid):
        for f in r["fanouts"]:
            n += 1
            out.append({"id": f"F{n}", "run": r["run"], "query": f})
    return out


def host_counts(cid):
    return Counter(s["host"] for r in db.research_runs(cid) for s in r["sources"] if s["host"])


def competitors(cid, brief):
    d = db.artifact(cid, "dissection") or {}
    client = brief["client_name"].lower()
    seen, out = set(), []
    for w in d.get("winners", []):
        b = w["brand"]
        if b.lower() not in seen and client not in b.lower():
            seen.add(b.lower())
            out.append({"brand": b, "category": w.get("category", ""), "reason": w.get("reason", "")})
    return out


def answer_status(row):
    return (row["supported"] or "").strip().lower()


# ---------------------------------------------------------------- 2 research

def step_research(cid, brief):
    runs = int(brief.get("runs", 3))
    done = {r["run"] - 1 for r in db.research_runs(cid)}
    todo = [i for i in range(runs) if i not in done]

    def one_run(i):
        db.event(cid, "RESEARCH", f"Run {i + 1}: asking “{brief['target_query']}” with web search on")
        for attempt in range(2):
            r = llm.research_run(brief["target_query"], run_idx=i + attempt * 10)
            m = r["measured"]
            if m["search_calls"] >= 1 and m["source_records"] >= 3:   # gate
                r["run"] = i + 1
                db.save_research_run(cid, i, r)
                db.add_cost(cid, r["cost_usd"])
                db.event(cid, "RESEARCH", f"Run {i + 1}: {m['search_calls']} fan-out queries, "
                         f"{m['source_records']} sources across {m['distinct_hosts']} sites ✓", "ok")
                return
            db.event(cid, "RESEARCH", f"Run {i + 1} failed the gate (too few searches/sources), re-running", "warn")
        raise RuntimeError(f"research run {i + 1} kept failing the gate")

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one_run, todo))

    rs = db.research_runs(cid)
    counts = host_counts(cid)
    db.event(cid, "RESEARCH", "Dissecting the answers: winners, categories, source types")
    inputs = {
        "client": brief["client_name"], "query": brief["target_query"],
        "runs": [{"run": r["run"], "fanouts": r["fanouts"], "answer": r["answer"],
                  "sources": sorted({(s["host"], s["title"]) for s in r["sources"]})} for r in rs],
        "all_hosts": sorted(counts),
    }
    out, meta = llm.run_skill("research-dissector", inputs, llm.WORKER_MODEL,
                              ["winners", "brand_mentions", "client", "host_types"])
    db.add_cost(cid, meta["cost_usd"])
    db.put_artifact(cid, "dissection", out)
    named = out["client"].get("named_in_answer")
    db.event(cid, "RESEARCH", f"Dissection done: {len({w['brand'] for w in out['winners']})} winning brands; "
             f"{brief['client_name']} named in answers: {'yes' if named else 'no'} ✓", "ok")
    return "AUDIT"


# ---------------------------------------------------------------- 3 audit

def step_audit(cid, brief):
    d = db.artifact(cid, "dissection")
    fanouts = fanout_index(cid)
    valid_ids = {f["id"] for f in fanouts}
    counts = host_counts(cid)
    inputs = {
        "brief": {k: brief[k] for k in ("client_name", "client_url", "location", "target_query")},
        "fanouts": [{"id": f["id"], "query": f["query"]} for f in fanouts],
        "winners": d["winners"], "brand_mentions": d["brand_mentions"],
        "reasoning_patterns": d.get("reasoning_patterns", []),
        "top_hosts": [{"host": h, "records": n, "type": d["host_types"].get(h, "other")} for h, n in counts.most_common(30)],
    }
    for attempt in range(2):
        out, meta = llm.run_skill("llm-visibility-audit", inputs, llm.WORKER_MODEL,
                                  ["keyword_groups", "winners_categories", "pages_profiles"], salt=str(attempt) if attempt else "")
        db.add_cost(cid, meta["cost_usd"])
        rows = [(g["group"], r) for g in out["keyword_groups"] for r in g.get("rows", [])]
        good = [(g, r) for g, r in rows if set(r.get("evidence", [])) & valid_ids]   # gate: tied to a real fan-out
        dropped = len(rows) - len(good)
        if rows and dropped / len(rows) <= 0.3:
            break
        db.event(cid, "AUDIT", f"Audit gate: {dropped}/{len(rows)} rows not tied to a real fan-out, rebuilding", "warn")
    else:
        raise RuntimeError("audit rows could not be tied to the research")

    db.x("DELETE FROM audit_rows WHERE campaign_id=?", cid)
    for g, r in good:
        db.x("INSERT INTO audit_rows (campaign_id, grp, keyword, context, evidence) VALUES (?,?,?,?,?)",
             cid, g, r["keyword"], r.get("context", ""), db.dump([e for e in r["evidence"] if e in valid_ids]))
    db.put_artifact(cid, "audit", {k: out.get(k, []) for k in ("winners_categories", "pages_profiles", "key_findings")}
                    | {"dropped_rows": dropped})
    db.event(cid, "AUDIT", f"Audit built: {len(good)} keyword rows in {len(out['keyword_groups'])} groups, "
             f"{len(out['winners_categories'])} winner rows, {len(out['pages_profiles'])} pages/profiles"
             + (f" ({dropped} untraceable rows dropped)" if dropped else "") + " ✓", "ok")
    return "AWAIT_CLIENT"


def fill_from_profile(cid, brief):
    """Demo helper: answer the client's Yes/No the way the client would, from their profile."""
    facts = brief["client_facts"]
    nope = [t.lower() for t in facts.get("not_offered", [])]
    # what the client actually says about itself; first-hand notes are one customer's story, not claims
    known = " ".join(str(v) for k, v in facts.items() if k not in ("not_offered", "first_hand_notes"))
    stems = {w[:5] for w in re.findall(r"[a-z0-9$]+", (known + " " + brief["location"]).lower()) if len(w) > 2}
    for r in db.audit_rows(cid):
        text = r["keyword"].lower()
        words = [w for w in re.findall(r"[a-z0-9$]+", text) if len(w) > 2 and w not in ("and", "the", "for", "with")]
        if any(t in text for t in nope):
            answer, note = "No", "not true for us"
        elif words and sum(w[:5] in stems for w in words) * 2 >= len(words):   # at least half the words match our facts
            answer, note = "Yes", r["notes"] or ""
        else:
            answer, note = "No", "not something we can claim"
        db.x("UPDATE audit_rows SET supported=?, notes=? WHERE id=?", answer, note, r["id"])


# ---------------------------------------------------------------- 5 plan

def validate_plan(items, brief, valid_fanouts):
    errs = []
    want = {k: int(v) for k, v in brief["mix"].items() if int(v)}
    got = Counter(i.get("type") for i in items)
    for t in set(want) | set(got):
        if want.get(t, 0) != got.get(t, 0):
            errs.append(f"type {t}: need exactly {want.get(t, 0)}, got {got.get(t, 0)}")
    for i in items:
        for ex in brief.get("exclude_titles", []):
            if SequenceMatcher(None, i.get("title", "").lower(), ex.lower()).ratio() > 0.75:
                errs.append(f"“{i['title']}” repeats already-published “{ex}”")
        if not i.get("angle"):
            errs.append(f"“{i.get('title')}” has no angle")
        if not set(i.get("target_fanouts", [])) & valid_fanouts:
            errs.append(f"“{i.get('title')}” targets no real fan-out query")
    titles = [i.get("title", "").lower() for i in items]
    if len(set(titles)) != len(titles):
        errs.append("duplicate titles")
    return errs


def step_plan(cid, brief):
    rows = db.audit_rows(cid)
    fanouts = fanout_index(cid)
    valid = {f["id"] for f in fanouts}
    inputs = {
        "brief": {k: brief[k] for k in ("client_name", "client_url", "location", "target_query", "site_type")},
        "mix": {k: int(v) for k, v in brief["mix"].items() if int(v)},
        "supported": [{"keyword": r["keyword"], "group": r["grp"], "client_note": r["notes"]} for r in rows if answer_status(r) == "yes"],
        "not_supported": [r["keyword"] for r in rows if answer_status(r) == "no"],
        "competitors": competitors(cid, brief),
        "fanouts": [{"id": f["id"], "query": f["query"]} for f in fanouts],
        "exclude_titles": brief.get("exclude_titles", []),
        "client_facts": brief["client_facts"],
    }
    db.event(cid, "PLAN", f"Planning {sum(inputs['mix'].values())} articles from {len(inputs['supported'])} "
             f"confirmed services (excluding {len(inputs['not_supported'])} the client said No to)")
    for attempt in range(2):
        out, meta = llm.run_skill("topical-authority-builder", inputs, llm.WORKER_MODEL, ["items"], salt=str(attempt) if attempt else "")
        db.add_cost(cid, meta["cost_usd"])
        items = out["items"]
        errs = validate_plan(items, brief, valid)
        if not errs:
            break
        db.event(cid, "PLAN", "Plan gate failed: " + "; ".join(errs[:4]) + ". Sending back to the planner", "warn")
        inputs["previous_errors"] = errs
    else:
        db.event(cid, "PLAN", "Plan still has issues after a retry; the operator will see them: " + "; ".join(errs[:4]), "warn")

    db.x("DELETE FROM plan_items WHERE campaign_id=?", cid)
    for n, i in enumerate(items):
        db.x("INSERT INTO plan_items (campaign_id, idx, type, title, brief, status) VALUES (?,?,?,?,?,?)",
             cid, n, i.get("type"), i.get("title"), db.dump(i), "proposed")
    db.event(cid, "PLAN", f"Plan ready: {len(items)} titles" + (" ✓" if not errs else " (with warnings)"), "ok" if not errs else "warn")
    return "AWAIT_OPERATOR"


# ---------------------------------------------------------------- 7 write + check

def writing_context(cid, brief):
    d = db.artifact(cid, "dissection")
    rows = db.audit_rows(cid)
    client_host = llm.host_of(brief["client_url"])
    brand_hosts = {h for h, t in d["host_types"].items() if t == "brand_site"}
    approved, seen = [], set()
    for r in db.research_runs(cid):
        for s in r["sources"]:
            if s["url"] and s["url"] not in seen and s["host"] not in brand_hosts and s["host"] != client_host:
                seen.add(s["url"])
                approved.append({"url": s["url"], "title": s["title"]})
    return {
        "client": {"name": brief["client_name"], "location": brief["location"], **brief["client_facts"]},
        "supported": [r["keyword"] for r in rows if answer_status(r) == "yes"],
        "not_supported": [r["keyword"] for r in rows if answer_status(r) == "no"] + brief["client_facts"].get("not_offered", []),
        "research": {"answers": [{"run": r["run"], "answer": r["answer"][:4000]} for r in db.research_runs(cid)],
                     "fanouts": [{"id": f["id"], "query": f["query"]} for f in fanout_index(cid)]},
        "approved_sources": approved[:60],
        "operator_notes": (db.artifact(cid, "first_review") or {}).get("notes", ""),
        "rules": {"words": [int(brief["words_min"]), int(brief["words_max"])], "sources": int(brief["sources_required"])},
        "_check": {"words_min": int(brief["words_min"]), "words_max": int(brief["words_max"]),
                   "sources_required": int(brief["sources_required"]),
                   "approved_urls": {s["url"] for s in approved[:60]},
                   "forbidden_hosts": brand_hosts | {client_host}},
    }


def write_one(cid, item, ctx, other_titles):
    art = db.one("SELECT * FROM articles WHERE plan_item_id=?", item["id"])
    aid = art["id"]
    attempts = art["attempts"] or []
    feedback = attempts[-1]["feedback"] if attempts else None
    label = f"#{item['idx'] + 1}"
    public = {k: v for k, v in ctx.items() if k != "_check"}

    def spend(meta):
        db.add_cost(cid, meta["cost_usd"])
        cur = db.one("SELECT cost_usd, seconds FROM articles WHERE id=?", aid)
        db.update_article(aid, cost_usd=cur["cost_usd"] + meta["cost_usd"], seconds=cur["seconds"] + meta["seconds"])

    while len(attempts) <= MAX_RETRIES:
        n = len(attempts)
        db.update_article(aid, status="writing" if n == 0 else f"rewriting ({n}/{MAX_RETRIES})")
        db.event(cid, "WRITE", f"{label} {'writing' if n == 0 else f'retry {n}: fixing'} “{item['title']}” (fresh {llm.WRITER_MODEL} writer)")
        article, meta = llm.run_skill("geo-content-writer", {**public, "brief": item["brief"] | {"title": item["title"]},
                                                             "feedback": feedback,
                                                             "previous_article": attempts[-1]["article"] if feedback else None},
                                      llm.WRITER_MODEL, ["title", "meta_description", "body_markdown", "sources"],
                                      salt=str(n) if n else "")
        spend(meta)

        db.update_article(aid, status="rule check")
        rules = checks.rule_check(article, ctx["_check"])
        fails = checks.failed(rules)
        if fails and all(f["name"] == "meta_length" for f in fails):     # fix one line, not the article
            for k in range(2):
                fixed, meta = llm.run_skill("meta-fixer", {"meta_description": article["meta_description"],
                                                           "current_length": len(article["meta_description"])},
                                            llm.REVIEWER_MODEL, ["meta_description"], salt=str(k) if k else "")
                spend(meta)
                article["meta_description"] = fixed["meta_description"]
                if 155 <= len(article["meta_description"]) <= 160:
                    break
            rules = checks.rule_check(article, ctx["_check"])
            fails = checks.failed(rules)
            if not fails:
                db.event(cid, "WRITE", f"{label} meta description repaired on its own ✓", "ok")

        review = None
        if fails:
            feedback = ["Rule check failed: " + f["detail"] for f in fails]
            verdict = "rules failed"
            db.event(cid, "WRITE", f"{label} ✕ rule check: " + "; ".join(f["detail"] for f in fails), "warn")
        else:
            db.update_article(aid, status="reviewing")
            db.event(cid, "WRITE", f"{label} rules ✓, now with the reviewer ({llm.REVIEWER_MODEL}, separate context)")
            review, meta = llm.run_skill("campaign-reviewer", {
                "article": article, "brief": item["brief"] | {"title": item["title"]}, "client": public["client"],
                "supported": ctx["supported"], "not_supported": ctx["not_supported"],
                "research": ctx["research"], "other_titles": other_titles}, llm.REVIEWER_MODEL, ["pass", "criteria"],
                salt=str(n) if n else "")
            spend(meta)
            review = checks.settle_review(review, article)
            failed_c = [c for c in review["criteria"] if not c.get("pass")]
            verdict = "passed" if not failed_c else "review failed"
            feedback = None if verdict == "passed" else (
                [f"{c['name']}: {c.get('reason', '')}" + (f" Sentence: “{c['quote']}”." if c.get("quote") else "")
                 + (f" Fix: {c['fix']}" if c.get("fix") else "") for c in failed_c])
            if verdict != "passed":
                db.event(cid, "WRITE", f"{label} ✕ reviewer: " + ", ".join(c["name"] for c in failed_c), "warn")

        attempts.append({"n": n, "article": article, "rules": rules, "review": review,
                         "verdict": verdict, "feedback": feedback})
        db.update_article(aid, attempts=attempts)
        if verdict == "passed":
            db.update_article(aid, status="ready", final=article)
            db.event(cid, "WRITE", f"{label} ✓ ready: “{article['title']}” ({checks.word_count(article['body_markdown'])} words"
                     + (f", passed after {n} {'retry' if n == 1 else 'retries'}" if n else ", passed first time") + ")", "ok")
            return
    db.update_article(aid, status="flagged", final=attempts[-1]["article"])
    db.event(cid, "WRITE", f"{label} ⚠ still failing after {MAX_RETRIES} retries, flagged for the operator", "error")


def write_batch(cid, brief, first_only):
    items = sorted(db.plan_items(cid, "approved"), key=lambda i: i["idx"])
    for it in items:
        db.x("INSERT OR IGNORE INTO articles (campaign_id, plan_item_id, status) VALUES (?,?,?)", cid, it["id"], "queued")
    done = {a["plan_item_id"] for a in db.articles(cid) if a["status"] in ("ready", "flagged", "accepted")}
    batch = items[:FIRST_N] if first_only else items
    todo = [it for it in batch if it["id"] not in done]
    ctx = writing_context(cid, brief)
    titles = [it["title"] for it in items]
    step = "WRITE_FIRST" if first_only else "WRITE"
    db.event(cid, step, f"Writing {'the first ' if first_only else ''}{len(todo)} articles, one fresh writer each, "
             f"{PARALLEL_WRITERS} at a time" + (" (with the operator's notes)" if ctx["operator_notes"] and not first_only else ""))
    with ThreadPoolExecutor(max_workers=PARALLEL_WRITERS) as ex:
        futures = [ex.submit(write_one, cid, it, ctx, [t for t in titles if t != it["title"]]) for it in todo]
        for f in futures:
            f.result()


def step_write_first(cid, brief):
    write_batch(cid, brief, first_only=True)
    return "AWAIT_FIRST"


def step_write(cid, brief):
    write_batch(cid, brief, first_only=False)
    return "FINAL_CHECK"


# ---------------------------------------------------------------- 9 final check

def step_final_check(cid, brief):
    arts = [{"id": a["id"], "title": a["final"]["title"], "body_markdown": a["final"]["body_markdown"]}
            for a in db.articles(cid) if a["final"]]
    issues = checks.set_check(arts)
    db.put_artifact(cid, "final_check", {"issues": issues, "articles": len(arts)})
    if issues:
        db.event(cid, "FINAL_CHECK", f"Set check: {len(issues)} repeat(s) across articles flagged for the operator", "warn")
    else:
        db.event(cid, "FINAL_CHECK", f"Set check: no repeated phrasing, headings or intros across {len(arts)} articles ✓", "ok")
    return "DONE"


HANDLERS = {"RESEARCH": step_research, "AUDIT": step_audit, "PLAN": step_plan,
            "WRITE_FIRST": step_write_first, "WRITE": step_write, "FINAL_CHECK": step_final_check}
