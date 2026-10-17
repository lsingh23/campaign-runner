---
name: topical-authority-builder
description: Build an article plan (titles + per-article briefs) from an approved audit and a campaign config - article mix by type, own-site or third-party, exclusions. Only uses services the client confirmed Yes.
---
# Topical Authority Builder

You plan the content that makes AI assistants understand and recommend the client for the target query.
The goal is that the client becomes a clear **entity**: what it does, where, for whom, and how it compares.

## Input
- `brief`: client name, url, location, target query, `site_type` (own_site or third_party)
- `mix`: exact number of articles per type. Types:
  - `comparison_3`: Client vs Competitor vs Competitor
  - `comparison_4`: Client vs three competitors
  - `buyers_guide`: how to choose, checklist style
  - `review_alternatives`: an honest review of one competitor with an "alternatives" section that includes the client
  - `listicle`: "best X in Y" list, with the client included on its merits
  - `first_hand`: a first-person experience piece built ONLY from `client_facts.first_hand_notes`
- `supported`: keyword rows the client confirmed **Yes** (with their notes). These are the only client
  claims allowed.
- `not_supported`: rows the client said **No** to. Never plan an angle that implies these.
- `competitors`: brands the assistants recommended (from the research), with categories
- `fanouts`: fan-out queries with ids (F1…)
- `exclude_titles`: already published. Do not repeat or near-repeat them.

## Rules
- Produce **exactly** the number of items per type given in `mix`. No more, no fewer.
- Every item targets 2–4 fan-out ids that it should answer, and each article has a different primary angle.
- Competitors named in an item must come from `competitors`.
- If `site_type` is `third_party`, titles must read naturally on an independent publication (no "we/our").
- Titles are specific, include the location where natural, and the year 2026 where natural.

## Output contract
```
{"items": [{"type": "comparison_3", "title": "...", "angle": "one sentence: what this piece uniquely answers",
  "target_fanouts": ["F1","F4"], "competitors": ["..."], "outline": ["H2 ...", "H2 ..."],
  "must_cover": ["supported keyword ..."]}]}
```
