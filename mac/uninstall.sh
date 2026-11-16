#!/bin/bash
# Stop and remove the Sigma DFS background updater (your code and data stay).
PLIST="$HOME/Library/LaunchAgents/com.sigmadfs.updater.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Sigma DFS updater stopped and removed."
