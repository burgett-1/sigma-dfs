#!/bin/bash
# Started by launchd. Runs the live pipeline during NBA DFS hours (8am to ~2am
# Eastern); outside them it checks back every 10 minutes. Polls every 60s, so
# injuries and lineups are picked up within about a minute, and publishes to the
# `data` branch, which the app reads directly.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
ENV_FILE="$HOME/.sigma-dfs.env"
# private channel the site uses to ask this Mac to run simulations
if ! grep -q '^CONTROL_TOPIC=' "$ENV_FILE" 2>/dev/null; then
  echo "CONTROL_TOPIC=sigma-ctl-$(openssl rand -hex 8)" >> "$ENV_FILE"
fi
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
export PYTHONUNBUFFERED=1

while true; do
  HOUR_ET=$(TZ=America/New_York date +%H)
  if [ "$HOUR_ET" -ge 8 ] || [ "$HOUR_ET" -lt 2 ]; then
    # pick up code updates you pushed to main (data lives on its own branch)
    git pull --quiet --ff-only origin main 2>/dev/null || true
    # keep the Mac awake while the pipeline runs (released when it exits)
    caffeinate -i .venv/bin/python -m pipeline.live --minutes 30 || sleep 15
  else
    sleep 600
  fi
done
