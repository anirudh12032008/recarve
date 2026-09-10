#!/bin/bash
# Launcher for the recarve web server, used by the LaunchAgent.
# Exists because launchd starts with a bare environment: no PATH for ffmpeg,
# and no way to read .env, which holds the API key, the session secret and
# the live database URL.
cd /Users/anirudh/Developer/recarve || exit 1
export PATH="/opt/homebrew/bin:/opt/homebrew/opt/postgresql@17/bin:/usr/bin:/bin:/usr/sbin:/sbin"
set -a
[ -f ./.env ] && . ./.env
set +a
export RECARVE_DB_URL="${RECARVE_DB_URL:-postgresql:///recarve}"
exec ./.venv/bin/python notes.py serve --port 8000
