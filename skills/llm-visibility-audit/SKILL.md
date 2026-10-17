---
name: llm-visibility-audit
description: Build an LLM visibility audit for a client from a research dissection - grouped keyword list with context and a blank supported? Yes/No column for the client, winners & categories, and pages/profiles to create.
---
# LLM Visibility Audit

You turn a research dissection into the audit an agency sends to its client. The client will answer
**"supported?" Yes/No** for each keyword row, meaning "do we actually offer this / is this true of us?".
Only rows they answer Yes to can be used in content later. Your rows must therefore be **concrete services,
attributes or claims a buyer cares about**, things a firm can honestly say Yes or No to.

## Input
`brief` (client, url, location, target query), `fanouts` (every fan-out query, each with an id like F3),
`winners`, `brand_mentions`, `reasoning_patterns`, `top_hosts`.

## What to produce
1. **keyword_groups**: 4–6 semantic groups (e.g. "Core services and scope", "Software and platforms",
   "Qualifications, registration and compliance", "Pricing and package transparency",
   "Reviews, reputation and comparison signals", "Location and service area"). Each group has 3–6 keyword
   rows. Each row has:
   - `keyword`: short phrase (e.g. "BAS lodgement", "Xero certified", "fixed monthly pricing")
   - `context`: one sentence on why the AI cares about it, citing what it searched for or rewarded
   - `evidence`: 1–3 fan-out ids (e.g. ["F2","F7"]) the row came from. **Every row must cite at least one
     real fan-out id from the input.** Rows you cannot tie to a fan-out are not allowed.
2. **winners_categories**: one row per (run, winning brand): run, category_type (Positive / Neutral / Budget /
   Specialist), exact_category, winner.
3. **pages_profiles**: 4–8 off-site pages or profiles the client should exist on, based on hosts the
   assistants actually used (directories, review sites, associations), each with `host`, `page`, and `why`.
4. **key_findings**: carry forward the 2–4 most important findings, adding anything the keyword view shows.

## Output contract
```
{"keyword_groups": [{"group": "...", "rows": [{"keyword": "...", "context": "...", "evidence": ["F1"]}]}],
 "winners_categories": [{"run": 1, "category_type": "Positive", "exact_category": "...", "winner": "..."}],
 "pages_profiles": [{"host": "...", "page": "...", "why": "..."}],
 "key_findings": ["..."]}
```
