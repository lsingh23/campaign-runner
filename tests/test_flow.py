"""End-to-end run of the whole pipeline through the web app, with the models stubbed out.
Covers both human check-ins, a reviewer fail -> retry -> pass, a flagged article, and every page."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["CAMPAIGN_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")
sys.path.insert(0, str(Path(__file__).parent.parent))

import llm  # noqa: E402

URLS = [f"https://site{i}.com.au/guide" for i in range(8)]
REVIEW_CALLS = {}
WRITER_NOTES = {}


def fake_research(query, model="x", run_idx=0):
    return {"run": run_idx + 1, "model": model, "query": query,
            "fanouts": [f"{query} reviews", f"{query} xero", f"{query} pricing"],
            "sources": [{"title": f"Guide {i}", "url": u, "host": llm.host_of(u), "query": query} for i, u in enumerate(URLS)]
            + [{"title": "Bookkept", "url": "https://bookkept.com.au", "host": "bookkept.com.au", "query": query}],
            "answer": "1. Bookkept: lots of reviews. 2. Ledgerly: Xero partner.",
            "measured": {"search_calls": 3, "source_records": 9, "distinct_hosts": 9, "answer_chars": 50, "seconds": 1},
            "cost_usd": 0.01, "cached": False}


def article(title, words=950, meta_len=157):
    body = "Answer first. " + " ".join(f"w{title[:3]}{i}" for i in range(words)) + \
           "".join(f" [s]({u})" for u in URLS[:4])
    return {"title": title, "meta_description": "m" * meta_len, "body_markdown": body,
            "sources": [{"url": u, "title": "t"} for u in URLS[:4]], "target_fanouts_answered": ["F1"]}


def fake_skill(skill, inputs, model, required=(), salt=""):
    meta = {"model": model, "cost_usd": 0.01, "seconds": 1, "cached": False}
    if skill == "research-dissector":
        return {"winners": [{"run": 1, "rank": 1, "brand": "Bookkept", "category": "Best overall", "reason": "reviews"},
                            {"run": 1, "rank": 2, "brand": "Ledgerly", "category": "Best for Xero", "reason": "partner"}],
                "brand_mentions": [], "client": {"named_in_answer": False, "seen_in_sources": False, "note": "absent"},
                "host_types": {"bookkept.com.au": "brand_site"}, "reasoning_patterns": [], "key_findings": ["k"]}, meta
    if skill == "llm-visibility-audit":
        return {"keyword_groups": [{"group": "Core", "rows": [
                    {"keyword": "BAS lodgement", "context": "c", "evidence": ["F1"]},
                    {"keyword": "Tax return preparation", "context": "c", "evidence": ["F2"]},
                    {"keyword": "Xero certified", "context": "c", "evidence": ["F3"]},
                    {"keyword": "made up row", "context": "c", "evidence": ["F999"]}]}],
                "winners_categories": [], "pages_profiles": [], "key_findings": []}, meta
    if skill == "topical-authority-builder":
        assert "Tax return preparation" in inputs["not_supported"]
        return {"items": [{"type": t, "title": f"Title {t} {n}", "angle": "a", "target_fanouts": ["F1"], "competitors": ["Bookkept"]}
                          for t, k in inputs["mix"].items() for n in range(k)]}, meta
    if skill == "geo-content-writer":
        t = inputs["brief"]["title"]
        assert (inputs["previous_article"] is None) == (inputs["feedback"] is None)   # retries revise the last draft
        WRITER_NOTES[t] = inputs["operator_notes"]
        if "listicle" in t:               # always too short -> flagged after retries
            return article(t, words=100), meta
        return article(t, meta_len=150 if "buyers" in t and not salt else 157), meta
    if skill == "meta-fixer":
        return {"meta_description": "m" * 158}, meta
    if skill == "campaign-reviewer":
        t = inputs["article"]["title"]
        REVIEW_CALLS[t] = REVIEW_CALLS.get(t, 0) + 1
        ok = not ("comparison_3 0" in t and REVIEW_CALLS[t] == 1)   # first comparison fails review once
        return {"pass": ok, "criteria": [{"name": "facts_traceable", "pass": ok, "reason": "r",
                                          "quote": "" if ok else "Answer first. wTit0 wTit1 wTit2", "fix": "fix the claim"},
                                         {"name": "quality", "pass": True, "reason": "r"},
                                         {"name": "client_claims_allowed", "pass": False, "reason": "made up",
                                          "quote": "a sentence the article never had"}]}, meta
    raise AssertionError(skill)


llm.research_run = fake_research
llm.run_skill = fake_skill

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import db  # noqa: E402


def wait_state(cid, states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        c = db.campaign(cid)
        assert not c["error"], c["error"]
        if c["state"] in states:
            return c
        time.sleep(0.1)
    raise AssertionError(f"stuck in {db.campaign(cid)['state']}")


class Flow(unittest.TestCase):
    def test_full_campaign(self):
        with TestClient(app.app) as cl:
            form = {"client_name": "Harbourline Bookkeeping", "client_url": "https://harbourlinebookkeeping.com.au",
                    "location": "Melbourne", "target_query": "best bookkeeping service melbourne", "site_type": "own_site",
                    "runs": "2", "mix_comparison_3": "2", "mix_buyers_guide": "1", "mix_listicle": "1",
                    "words_min": "900", "words_max": "1100", "sources_required": "4", "exclude_titles": "",
                    "about": "a", "pricing": "p", "services": "bookkeeping", "credentials": "BAS agent",
                    "not_offered": "tax return, audit", "first_hand_notes": "n"}
            r = cl.post("/new", data=form, follow_redirects=False)
            cid = int(r.headers["location"].rsplit("/", 1)[1])

            c = wait_state(cid, {"AWAIT_CLIENT"})
            self.assertEqual(len(db.audit_rows(cid)), 3)            # untraceable row dropped by the gate
            for u in ("/c/%d" % cid, "/c/%d/live" % cid, "/c/%d/research" % cid, "/c/%d/audit" % cid, "/c/%d/audit.csv" % cid):
                self.assertEqual(cl.get(u).status_code, 200, u)

            tok = c["client_token"]
            self.assertEqual(cl.get(f"/client/{tok}").status_code, 200)
            cl.post(f"/client/{tok}", data={"action": "fill"})
            self.assertEqual({r["keyword"]: r["supported"] for r in db.audit_rows(cid)}["Tax return preparation"], "No")
            cl.post(f"/client/{tok}", data={"action": "submit", **{f"s_{r['id']}": r["supported"] for r in db.audit_rows(cid)}})

            wait_state(cid, {"AWAIT_OPERATOR"})
            self.assertEqual(cl.get(f"/c/{cid}/plan").status_code, 200)
            items = db.plan_items(cid)
            self.assertEqual(len(items), 4)
            data = {f"t_{i['id']}": i["title"] for i in items} | {f"k_{i['id']}": "on" for i in items}
            cl.post(f"/c/{cid}/plan", data=data)

            wait_state(cid, {"AWAIT_FIRST"})                         # operator reads the first 2 before the rest
            self.assertEqual(sum(1 for a in db.articles(cid) if a["final"]), 2)
            self.assertEqual(cl.get(f"/c/{cid}/live").status_code, 200)
            cl.post(f"/c/{cid}/first", data={"notes": "shorter intros"})

            wait_state(cid, {"DONE"})
            first = {"Title comparison_3 0", "Title comparison_3 1"}
            self.assertEqual({t for t, n in WRITER_NOTES.items() if n == "shorter intros"}, set(WRITER_NOTES) - first)
            self.assertTrue(all(WRITER_NOTES[t] == "" for t in first))
            arts = {a["plan_title"]: a for a in db.articles(cid)}
            self.assertEqual(arts["Title comparison_3 0"]["status"], "ready")
            self.assertEqual(len(arts["Title comparison_3 0"]["attempts"]), 2)      # failed review, then passed
            self.assertEqual(arts["Title buyers_guide 0"]["status"], "ready")       # meta repaired, no rewrite
            self.assertEqual(len(arts["Title buyers_guide 0"]["attempts"]), 1)
            self.assertEqual(arts["Title listicle 0"]["status"], "flagged")
            self.assertEqual(len(arts["Title listicle 0"]["attempts"]), 3)
            self.assertIsNotNone(db.artifact(cid, "final_check"))
            for a in arts.values():
                self.assertEqual(cl.get(f"/c/{cid}/articles/{a['id']}").status_code, 200)
            self.assertEqual(cl.get(f"/c/{cid}/articles").status_code, 200)
            z = cl.get(f"/c/{cid}/export.zip")
            self.assertEqual(z.status_code, 200)
            self.assertEqual(cl.get(f"/c/{cid}/live").status_code, 200)


if __name__ == "__main__":
    unittest.main()
