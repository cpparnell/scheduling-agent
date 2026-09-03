#!/usr/bin/env bash
# Installs scheduling-agent as a launchd agent: starts on login, restarts on
# crash (KeepAlive). Full Disk Access must still be granted manually — macOS
# gives no scriptable way to do that.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PROJECT_DIR="$(pwd)"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
LOG_DIR="$HOME/Library/Logs/scheduling-agent"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="com.scheduling-agent.plist"
DEST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "No venv found at $PYTHON_BIN — run ./scripts/setup.sh first." >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

sed \
    -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" \
    -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
    -e "s#__LOG_DIR__#${LOG_DIR}#g" \
    scripts/com.scheduling-agent.plist > "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed and loaded $DEST"
echo "It will start on login and restart automatically if it crashes."
echo
echo "Useful commands:"
echo "  launchctl list | grep scheduling-agent   # check it's running"
echo "  launchctl unload $DEST                   # stop and disable"
echo "  tail -f $LOG_DIR/launchd.log              # supervisor-level output"
echo
echo "If you haven't already, grant Full Disk Access to:"
echo "  $PYTHON_BIN"
echo "at System Settings > Privacy & Security > Full Disk Access"
