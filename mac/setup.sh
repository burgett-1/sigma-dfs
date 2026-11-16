#!/bin/bash
# Sigma DFS updater for macOS: runs the live projection pipeline from this Mac
# (DraftKings and NBA.com serve normal home connections) and publishes to your
# site's `data` branch. Installs a background job that runs on game days.
#
#   cd ~/sigma-dfs && bash mac/setup.sh
#
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
echo "Sigma DFS updater setup in $REPO"

# 1) Python 3.10+
PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

# 2) private environment with the pipeline's packages
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Packages installed."

# 3) the data branch, checked out next to the code (the pipeline writes here, then pushes)
git fetch --quiet origin
if [ ! -d data/.git ] && [ ! -f data/.git ]; then
  if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
    git fetch --quiet origin data
    git worktree add -f -B data data FETCH_HEAD >/dev/null
  else
    git worktree add --detach data >/dev/null
    git -C data checkout --orphan data
    git -C data rm -rfq . || true
  fi
fi
echo "Data folder ready."

# 4) check this Mac can push to GitHub (uses your normal GitHub sign-in)
if ! git push --dry-run origin HEAD:refs/heads/sigma-push-check >/dev/null 2>&1; then
  echo
  echo "This Mac can't push to GitHub yet. Sign in once, e.g. with GitHub Desktop"
  echo "(https://desktop.github.com) or 'gh auth login', then run this script again."
  exit 1
fi
echo "GitHub access OK."

# 5) phone alerts topic (same one as in the ntfy app)
ENV_FILE="$HOME/.sigma-dfs.env"
if [ ! -f "$ENV_FILE" ]; then
  read -r -p "Your ntfy topic (the name you subscribed to in the ntfy app): " TOPIC
  printf 'NTFY_TOPIC=%s\nSITE_URL=%s\n' "$TOPIC" "https://$(git remote get-url origin | sed -E 's#.*github.com[:/]([^/]+)/([^/.]+).*#\1.github.io/\2#')/" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

# 6) background job (starts now and at every login)
PLIST="$HOME/Library/LaunchAgents/com.sigmadfs.updater.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" mac/com.sigmadfs.updater.plist > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Done. The updater is running."
echo "  Log:        tail -f ~/Library/Logs/sigma-dfs.log"
echo "  Stop it:    bash mac/uninstall.sh"
echo "It works while this Mac is on and awake on game days (it keeps the Mac awake while a slate is live)."
