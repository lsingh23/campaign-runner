import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import checks  # noqa: E402

APPROVED = {"https://ato.gov.au/a", "https://icb.org.au/b", "https://news.com.au/c", "https://smartcompany.com.au/d"}
RULES = {"words_min": 10, "words_max": 60, "sources_required": 4, "approved_urls": APPROVED,
         "forbidden_hosts": {"harbourlinebookkeeping.com.au", "bookkept.com.au"}}


def article(body, meta="x" * 157, sources=APPROVED):
    return {"body_markdown": body, "meta_description": meta, "sources": [{"url": u} for u in sources]}


def by_name(results):
    return {r["name"]: r["pass"] for r in results}


class ReviewGate(unittest.TestCase):
    art = {"title": "T", "meta_description": "m", "body_markdown": "Harbourline was **founded in 2017** in Richmond. "
           "It serves [all of Melbourne](https://x.com/a) for small business owners."}

    def test_quote_found_despite_markdown_and_ellipsis(self):
        self.assertTrue(checks.quote_in("Harbourline was founded in 2017 … serves all of Melbourne", self.art))
        self.assertTrue(checks.quote_in("“It serves all of Melbourne for small business owners.”", self.art))

    def test_quote_missing_or_empty(self):
        self.assertFalse(checks.quote_in("Harbourline scales past $500k revenue", self.art))
        self.assertFalse(checks.quote_in("", self.art))

    def test_settle_drops_only_unquoted_claim_fails(self):
        review = {"pass": False, "criteria": [
            {"name": "client_claims_allowed", "pass": False, "quote": "packages scale past $500k"},
            {"name": "facts_traceable", "pass": False, "quote": "founded in 2017 in Richmond"},
            {"name": "quality", "pass": False, "quote": ""}]}
        got = {c["name"]: c["pass"] for c in checks.settle_review(review, self.art)["criteria"]}
        self.assertEqual(got, {"client_claims_allowed": True, "facts_traceable": False, "quality": False})


class RuleCheck(unittest.TestCase):
    body = "Answer first. " + "word " * 30 + "[ATO](https://ato.gov.au/a)"

    def test_good_article_passes(self):
        self.assertEqual(checks.failed(checks.rule_check(article(self.body), RULES)), [])

    def test_word_count(self):
        self.assertFalse(by_name(checks.rule_check(article("too short"), RULES))["word_count"])

    def test_meta_length_bounds(self):
        self.assertFalse(by_name(checks.rule_check(article(self.body, meta="x" * 154), RULES))["meta_length"])
        self.assertTrue(by_name(checks.rule_check(article(self.body, meta="x" * 160), RULES))["meta_length"])
        self.assertFalse(by_name(checks.rule_check(article(self.body, meta="x" * 161), RULES))["meta_length"])

    def test_client_and_competitor_links(self):
        body = self.body + " [us](https://www.harbourlinebookkeeping.com.au/pricing) [them](https://bookkept.com.au)"
        r = by_name(checks.rule_check(article(body), RULES))
        self.assertFalse(r["no_client_competitor_links"])
        self.assertTrue(r["no_unapproved_links"])  # not double-reported

    def test_unapproved_link_and_source(self):
        r = by_name(checks.rule_check(article(self.body + " [x](https://random.com/y)",
                                              sources=list(APPROVED)[:3] + ["https://random.com/y"]), RULES))
        self.assertFalse(r["no_unapproved_links"])
        self.assertFalse(r["sources_approved"])

    def test_source_count(self):
        self.assertFalse(by_name(checks.rule_check(article(self.body, sources=list(APPROVED)[:3]), RULES))["source_count"])


class SetCheck(unittest.TestCase):
    def test_flags_repeats_not_distinct(self):
        shared = "the quick brown fox jumps over the lazy dog near the river bank today "
        a = {"id": 1, "body_markdown": "## Pricing\n" + shared * 3 + "alpha " * 50}
        b = {"id": 2, "body_markdown": "## Pricing\n" + "beta " * 50 + shared * 3}
        c = {"id": 3, "body_markdown": "## Something else\n" + " ".join(f"w{i}" for i in range(200))}
        issues = checks.set_check([a, b, c])
        pairs = {(i["a"], i["b"], i["kind"]) for i in issues}
        self.assertIn((1, 2, "repeated phrasing"), pairs)
        self.assertIn((1, 2, "identical headings"), pairs)
        self.assertFalse([i for i in issues if 3 in (i["a"], i["b"])])


if __name__ == "__main__":
    unittest.main()
