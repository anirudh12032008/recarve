#!/bin/bash
# Transcription worker. The VM has no GPU, so this Mac is the only machine that can
# run Whisper: it pulls queued lectures, transcribes, pushes notes back.
#
# It reaches the server through an SSH tunnel, not the public URL: Cloudflare blocks
# Python's user-agent as a bot (error 1010), and the worker endpoints are better off
# never being publicly reachable.
#
# The tunnel and the worker must live and die together. An earlier version exec'd the
# worker, which left nothing watching the tunnel — when the laptop slept, the tunnel
# died and the worker retried a dead port for hours while launchd saw a healthy
# process. So: run both as children, and exit as soon as either one goes, which lets
# launchd restart the pair cleanly.
cd /Users/anirudh/Developer/recarve || exit 1
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
set -a; [ -f ./.env ] && . ./.env; set +a
export SSL_CERT_FILE="/Users/anirudh/Developer/recarve/.venv/lib/python3.14/site-packages/certifi/cacert.pem"

# Set these for your own install, or put them in .env beside the API keys.
VM="${RECARVE_VM:?set RECARVE_VM=user@host, or put it in .env}"
KEY="${RECARVE_VM_KEY:-$HOME/.ssh/id_ed25519}"
PORT="${RECARVE_TUNNEL_PORT:-18420}"

ssh -N -L "${PORT}:127.0.0.1:8000" -i "$KEY" \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new "$VM" &
SSH_PID=$!

for _ in $(seq 1 25); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/" && break
  kill -0 "$SSH_PID" 2>/dev/null || { echo "tunnel failed to start"; exit 1; }
  sleep 1
done

./.venv/bin/python notes.py worker --server "http://127.0.0.1:${PORT}" &
PY_PID=$!

# bash 3.2 on macOS has no `wait -n`, so poll both.
while kill -0 "$SSH_PID" 2>/dev/null && kill -0 "$PY_PID" 2>/dev/null; do sleep 5; done
echo "tunnel or worker exited; stopping both so launchd restarts a fresh pair"
kill "$SSH_PID" "$PY_PID" 2>/dev/null
wait 2>/dev/null
