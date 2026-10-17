"""Rule checks done in code: instant, deterministic, no model involved."""
import re
from itertools import combinations

from llm import host_of

LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)|(?<![(\[])\b(https?://[^\s)\]]+)")


def word_count(md: str) -> int:
    text = re.sub(r"\]\([^)]*\)", "]", md)          # drop link targets
    text = re.sub(r"[#*_>|`\-]+", " ", text)
    return len(re.findall(r"\b[\w'’$%.,/]+\b", text))


def links_in(md: str) -> list[str]:
    return [a or b for a, b in LINK_RE.findall(md)]


def rule_check(article: dict, rules: dict) -> list[dict]:
    """rules: words_min, words_max, sources_required, approved_urls (set), forbidden_hosts (set).
    Returns a list of {name, pass, detail}."""
    body = article.get("body_markdown", "")
    meta = article.get("meta_description", "")
    sources = [s.get("url", "") for s in article.get("sources", [])]
    links = links_in(body)
    approved = rules["approved_urls"]
    forbidden = rules["forbidden_hosts"]
    wc = word_count(body)
    res = []

    def add(name, ok, detail):
        res.append({"name": name, "pass": bool(ok), "detail": detail})

    add("word_count", rules["words_min"] <= wc <= rules["words_max"],
        f"{wc} words (needs {rules['words_min']}–{rules['words_max']})")
    n = len(set(sources))
    add("source_count", n == rules["sources_required"], f"{n} sources listed (needs exactly {rules['sources_required']})")
    bad_src = [u for u in sources if u not in approved]
    add("sources_approved", not bad_src, "all from the approved research sources" if not bad_src
        else f"not in approved sources: {', '.join(bad_src[:3])}")
    bad_links = [u for u in links if host_of(u) in forbidden or any(host_of(u).endswith("." + f) for f in forbidden)]
    add("no_client_competitor_links", not bad_links, "no links to client or competitor sites" if not bad_links
        else f"links to client/competitor sites: {', '.join(bad_links[:3])}")
    stray = [u for u in links if u not in approved and u not in bad_links]
    add("no_unapproved_links", not stray, "every link is an approved source" if not stray
        else f"links not in approved sources: {', '.join(stray[:3])}")
    add("meta_length", 155 <= len(meta) <= 160, f"meta is {len(meta)} characters (needs 155–160)")
    return res


def failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["pass"]]


# ---------------------------------------------------------------- reviewer gate

QUOTE_REQUIRED = {"facts_traceable", "client_claims_allowed"}


def _norm(text: str) -> str:
    text = re.sub(r"\]\([^)]*\)", "]", text)                     # drop link targets
    text = text.translate(str.maketrans("‘’“”–—", "''\"\"--"))
    text = re.sub(r"[#*_>|`\[\]]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in(quote: str, article: dict) -> bool:
    """True if the reviewer's quote really is in the article. Ellipses split it into fragments; each
    fragment of 3+ words must appear."""
    haystack = _norm(" ".join([article.get("title", ""), article.get("meta_description", ""),
                               article.get("body_markdown", "")]))
    parts = [_norm(p).strip(" .,;:'\"") for p in re.split(r"\.\.\.|…", quote or "")]
    parts = [p for p in parts if len(p.split()) >= 3]
    return bool(parts) and all(p in haystack for p in parts)


def settle_review(review: dict, article: dict) -> dict:
    """A claim-level fail (facts, client claims) only counts if it quotes a sentence that is actually
    in the article. Other fails are dropped and kept on record, so the writer isn't sent after a claim that isn't there."""
    for c in review.get("criteria", []):
        if not c.get("pass") and c.get("name") in QUOTE_REQUIRED and not quote_in(c.get("quote", ""), article):
            c["pass"] = True
            c["dropped"] = "fail dropped: its quote is not in the article"
    return review


# ---------------------------------------------------------------- final check across the set

def _shingles(md: str, n: int = 8) -> set:
    words = re.findall(r"[a-z0-9']+", md.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _headings(md: str) -> set:
    return {re.sub(r"[^a-z0-9 ]", "", h.lower()).strip() for h in re.findall(r"^#{2,3}\s+(.+)$", md, re.M)}


def set_check(articles: list[dict], overlap_threshold: float = 0.04) -> list[dict]:
    """articles: [{id, title, body_markdown}]. Flags pairs that share phrasing or identical headings,
    i.e. the repeated 'footprints' an AI tends to leave across a campaign."""
    issues = []
    prepared = [(a, _shingles(a["body_markdown"]), _headings(a["body_markdown"])) for a in articles]
    for (a, sa, ha), (b, sb, hb) in combinations(prepared, 2):
        if sa and sb:
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= overlap_threshold:
                shared = sorted(sa & sb)[:2]
                issues.append({"a": a["id"], "b": b["id"], "kind": "repeated phrasing",
                               "detail": f"{overlap:.0%} of 8-word runs shared, e.g. “{shared[0]}”"})
        same = sorted(ha & hb)
        if same:
            issues.append({"a": a["id"], "b": b["id"], "kind": "identical headings",
                           "detail": "; ".join(same[:3])})
        intro_a, intro_b = _shingles(a["body_markdown"][:600], 5), _shingles(b["body_markdown"][:600], 5)
        if intro_a and intro_b and len(intro_a & intro_b) / min(len(intro_a), len(intro_b)) > 0.15:
            issues.append({"a": a["id"], "b": b["id"], "kind": "similar intros", "detail": "opening paragraphs overlap"})
    return issues
