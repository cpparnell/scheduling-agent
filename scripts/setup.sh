#!/usr/bin/env bash
# One-shot setup: venv, install, .env, Full Disk Access check, optional launchd install.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== scheduling-agent setup =="

if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "Installing scheduling-agent..."
pip install --quiet --upgrade pip
pip install --quiet -e .

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    read -r -p "Enter your Anthropic API key (sk-ant-...): " api_key
    if [ -n "$api_key" ]; then
        # macOS/BSD sed requires an explicit (empty) backup suffix with -i.
        sed -i '' "s/^ANTHROPIC_API_KEY=.*/ANTHROPIC_API_KEY=${api_key}/" .env
    else
        echo "Skipped — edit .env and set ANTHROPIC_API_KEY before running."
    fi
else
    echo ".env already exists, leaving it as-is."
fi

echo
echo "Checking Full Disk Access for $(which python3)..."
if python3 -c "
import sqlite3, pathlib, sys
db = pathlib.Path.home() / 'Library' / 'Messages' / 'chat.db'
try:
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)
    con.execute('select 1 from message limit 1')
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    echo "  looks good — chat.db is readable."
else
    echo "  could not read chat.db. Grant Full Disk Access to your terminal app at:"
    echo "  System Settings > Privacy & Security > Full Disk Access"
fi

echo
read -r -p "Install a launchd agent so scheduling-agent runs in the background and restarts on crash/login? [y/N] " install_agent
if [[ "$install_agent" =~ ^[Yy]$ ]]; then
    ./scripts/install-launchagent.sh
else
    echo "Skipped. Run './scripts/install-launchagent.sh' later, or start manually with 'scheduling-agent'."
fi

echo
echo "Setup complete."
