# Campaign Runner (v1 demo)

This is a small working version of the LLM ranking campaign pipeline. It runs one campaign end to end, from brief to finished articles, and stops at the three points where a person should decide.

```
Brief ─► Research ─► Audit ─► CLIENT Yes/No ─► Plan ─► OPERATOR OKs titles ─► Write first 2 ─► OPERATOR OKs first 2 ─► Write the rest ─► Final set check ─► Ready
          3 runs     sheet     (waits)          only    (waits)                 1 fresh writer    (waits; notes go to     same line,        repeats across
          web search                            "Yes"                           rules in code     the remaining writers)  + operator notes  the whole set
                                                                                + other-model reviewer
                                                                                retry ×2 → operator
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn jinja2 python-multipart markdown
.venv/bin/uvicorn app:app --port 8000            # open http://127.0.0.1:8000
```

Models are called through `claude -p` (headless Claude Code) using your existing Claude login, so no API key is needed.

| env | default | |
|---|---|---|
| `LLM_MODE` | `live` | `live` reuses identical earlier calls from `data/cache`. `fresh` always calls the model. `replay` uses the cache only |
| `WRITER_MODEL` / `REVIEWER_MODEL` | `haiku` / `sonnet` | the cheapest model writes; the reviewer must be a different model (the app refuses to start otherwise) |
| `RESEARCH_MODEL` / `WORKER_MODEL` | `haiku` | research captures, and the audit + plan |

For a demo, run the seed campaign once in `live` mode first, which fills `data/cache` (the cache is not in this repo). Then `./demo.sh` starts the app in `replay` mode: create the same campaign and use the "Demo: answer from client profile" button to replay the whole run.

## What maps to what

| Agency today | Here |
|---|---|
| `chatgpt-backend-dissector` | `llm.research_run` captures the fan-out searches, sources and answer from a web-search run. `skills/research-dissector` then extracts the winners and host types |
| LLM Visibility Audit (GPT skill) → Google Sheet | `skills/llm-visibility-audit` → audit page + CSV + client sheet page |
| Client fills column C "supported?" | `/client/<token>`: the pipeline waits until it is submitted |
| Topical Authority Builder + long custom prompt | `skills/topical-authority-builder` + the brief form (mix, site type, exclusions) |
| "Next 4 articles" × 8 | one fresh writer call per article, 3 in parallel (`skills/geo-content-writer`) |
| Same-agent QC | `checks.py` (rules in code) + `skills/campaign-reviewer` on a different model |
| Footprint check vs earlier articles | `checks.set_check` across the whole set |

The skills are ordinary Claude `SKILL.md` files. Copy any folder into `~/.claude/skills/` to use it by hand in Claude, or replace it with the agency's real skill: the app only relies on each skill's output contract.

## Files

- `conductor.py`: the step order, the gates, retries and resume
- `db.py`: the campaign record (SQLite), saved after every step
- `llm.py`: the model adapter and cache
- `checks.py`: the rule checks
- `app.py` + `templates/`: the web UI
- `tests/`: `checks` unit tests, plus a full end-to-end flow with stubbed models (`.venv/bin/python -m unittest discover tests`)

## Not in v1 (the roadmap)

- Measure again in 2–4 weeks, and the GEO Brain learning loop
- Real Google Sheets sync
- Ahrefs DR
- ChatGPT, Gemini and AI Mode as separate platforms. The OpenAI Responses `web_search` tool and Gemini grounding both expose the search queries, so each can sit behind `research_run`
- A job queue and workflow engine for many campaigns at once
