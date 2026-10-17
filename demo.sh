#!/bin/sh
# Start the demo with a clean slate. It replays a real recorded run; each AI call waits 15% of the
# time it really took (REPLAY_PACE=0.15; set REPLAY_PACE=1 for real time). Open http://127.0.0.1:8000 once it says "Uvicorn running".
cd "$(dirname "$0")"
pkill -f "uvicorn app:app" 2>/dev/null
rm -f data/demo.db
LLM_MODE=${LLM_MODE:-replay} REPLAY_PACE=${REPLAY_PACE:-0.15} CAMPAIGN_DB=data/demo.db exec .venv/bin/uvicorn app:app --port 8000
