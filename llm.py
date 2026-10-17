"""Model adapter. Every model call goes through `claude -p` (headless Claude Code,
using the operator's existing login), and every call is cached on disk.

LLM_MODE:
  live   (default) use the cache when the exact same call was made before, else call the model
  fresh  always call the model (still writes the cache)
  replay never call the model; serve from cache only (for the live demo)

REPLAY_PACE (replay only): wait this fraction of each call's originally recorded duration,
so the demo moves at a believable speed. 0.15 turns a 70 s article into ~10 s. Unset: ~1 s per call.
"""
import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MODE = os.environ.get("LLM_MODE", "live")
REPLAY_PACE = float(os.environ.get("REPLAY_PACE") or 0)
# the cheapest model everywhere except the reviewer, which is always a different model from the writer
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "haiku")
WORKER_MODEL = os.environ.get("WORKER_MODEL", "haiku")      # audit + plan
WRITER_MODEL = os.environ.get("WRITER_MODEL", "haiku")
REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "sonnet")
if REVIEWER_MODEL == WRITER_MODEL:
    raise SystemExit("REVIEWER_MODEL must differ from WRITER_MODEL: the writer must not review its own work")
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "900"))

BASE_ARGS = ["--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
_WORKDIR = tempfile.mkdtemp(prefix="campaign-runner-")


class LLMError(RuntimeError):
    pass


def _call(args: list[str], prompt: str) -> tuple[str, bool]:
    """Run `claude -p` with args, prompt on stdin. Returns (stdout, from_cache)."""
    key = hashlib.sha256(json.dumps([args, prompt]).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.out"
    if MODE != "fresh" and path.exists():
        out = path.read_text()
        if MODE == "replay":
            if REPLAY_PACE:
                time.sleep(max(1.0, _recorded_seconds(out) * REPLAY_PACE * random.uniform(0.85, 1.15)))
            else:
                time.sleep(random.uniform(0.6, 1.8))  # so the timeline still visibly moves
        return out, True
    if MODE == "replay":
        raise LLMError("replay mode: this call is not in the cache (run the campaign live once first)")
    proc = subprocess.run(
        ["claude", "-p", *BASE_ARGS, *args],
        input=prompt, capture_output=True, text=True, timeout=TIMEOUT, cwd=_WORKDIR,
    )
    if proc.returncode != 0:
        raise LLMError(f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
    path.write_text(proc.stdout)
    return proc.stdout, False


def _recorded_seconds(raw: str) -> float:
    """How long the original call took, from the result envelope (the last line for stream-json)."""
    for line in reversed(raw.strip().splitlines()):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and "duration_ms" in ev:
            return ev["duration_ms"] / 1000
    return 1.0


# ---------------------------------------------------------------- skills

def load_skill(name: str) -> str:
    """A skill is a Claude-style SKILL.md; the body (after frontmatter) is the system prompt."""
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def run_skill(skill: str, inputs: dict, model: str, required: list[str] = (), salt: str = "") -> tuple[dict, dict]:
    """Run one skill as a fresh, tool-less model call. Returns (output, meta).
    The output must be one JSON object containing every key in `required`."""
    system = load_skill(skill)
    prompt = (
        f"INPUT (JSON):\n{json.dumps(inputs, indent=1, ensure_ascii=False)}\n\n"
        "Reply with ONE JSON object only, exactly as the output contract describes. No prose, no code fences."
    )
    if salt:
        prompt += f"\n\n(attempt {salt})"
    args = ["--model", model, "--tools", "", "--system-prompt", system, "--output-format", "json"]
    last_err = None
    for attempt in range(2):
        p = prompt if attempt == 0 else prompt + f"\n\nYour previous reply could not be used: {last_err}. Return valid JSON only."
        raw, cached = _call(args, p)
        envelope = json.loads(raw)
        meta = {"model": model, "cost_usd": envelope.get("total_cost_usd", 0) if not cached else 0,
                "seconds": round(envelope.get("duration_ms", 0) / 1000, 1), "cached": cached}
        if envelope.get("is_error"):
            last_err = envelope.get("result", "error")
            continue
        try:
            out = _extract_json(envelope.get("result", ""))
            missing = [k for k in required if k not in out]
            if missing:
                raise ValueError(f"missing keys {missing}")
            return out, meta
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            last_err = str(e)
    raise LLMError(f"{skill}: no usable output ({last_err})")


# ---------------------------------------------------------------- research

def host_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def research_run(query: str, model: str = RESEARCH_MODEL, run_idx: int = 0) -> dict:
    """Ask the query the way a user would, with web search on, and record what the
    model did behind the answer: fan-out queries, every source it saw, the answer."""
    prompt = (
        f"{query}\n\n"
        "Search the web before answering. Then give your top recommendations as a ranked list "
        "with a one-line reason for each, the way you would answer a real user."
    )
    if run_idx:
        prompt += f"\n\n(independent run #{run_idx + 1})"
    args = ["--model", model, "--tools", "WebSearch", "--allowedTools", "WebSearch",
            "--output-format", "stream-json", "--verbose"]
    raw, cached = _call(args, prompt)

    fanouts, sources, answer, result = [], [], "", {}
    query_by_tool = {}
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev["message"].get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "WebSearch":
                    q = block["input"].get("query", "")
                    fanouts.append(q)
                    query_by_tool[block["id"]] = q
                elif block.get("type") == "text":
                    answer = block["text"]
        elif ev.get("type") == "user":
            for block in ev["message"].get("content", []):
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                m = re.search(r"Links:\s*(\[.*?\])\s*(?:\n|$)", content or "", re.S)
                if not m:
                    continue
                try:
                    links = json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
                for link in links:
                    sources.append({"title": link.get("title", ""), "url": link.get("url", ""),
                                    "host": host_of(link.get("url", "")),
                                    "query": query_by_tool.get(block.get("tool_use_id"), "")})
        elif ev.get("type") == "result":
            result = ev
            answer = ev.get("result") or answer

    hosts = sorted({s["host"] for s in sources if s["host"]})
    return {
        "run": run_idx + 1,
        "model": model,
        "query": query,
        "fanouts": fanouts,
        "sources": sources,
        "answer": answer,
        "measured": {
            "search_calls": len(fanouts),
            "source_records": len(sources),
            "distinct_hosts": len(hosts),
            "answer_chars": len(answer),
            "seconds": round(result.get("duration_ms", 0) / 1000, 1),
        },
        "cost_usd": 0 if cached else result.get("total_cost_usd", 0),
        "cached": cached,
    }
