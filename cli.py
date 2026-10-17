"""Run one research capture from the terminal and print what the AI did behind its answer.
   .venv/bin/python cli.py research "best bookkeeping service melbourne"
"""
import sys
from collections import Counter

import llm


def research(query):
    r = llm.research_run(query)
    m = r["measured"]
    print(f"\n{query}  ·  {r['model']}  ·  {m['seconds']} s{'  (cached)' if r['cached'] else ''}")
    print(f"{m['search_calls']} fan-out searches · {m['source_records']} sources · {m['distinct_hosts']} sites\n")
    print("Fan-out queries:")
    for i, f in enumerate(r["fanouts"], 1):
        print(f"  F{i}  {f}")
    print("\nTop sites:")
    for h, n in Counter(s["host"] for s in r["sources"]).most_common(10):
        print(f"  {n:>3}  {h}")
    print("\nAnswer:\n" + r["answer"])


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "research":
        sys.exit(__doc__)
    research(" ".join(sys.argv[2:]))
