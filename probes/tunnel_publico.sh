#!/usr/bin/env bash
# Public tunnel (Cloudflare quick tunnel) to serve the conversation logs.
#
# The trycloudflare subdomain is RANDOM on every start: it is saved in
# ${CXP_LOGS_DIR:-<repo>/logs-conversaciones}/.url-publica so it can be copied.
#
# The origin HTTP service is served by the systemd unit `sirve-logs-conversaciones`
# on 127.0.0.1:8099.
set -uo pipefail

BIN=/tmp/cloudflared
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${CXP_LOGS_DIR:-$RAIZ/logs-conversaciones}"
OUT="$DIR/.url-publica"
LOG=/tmp/cloudflared.log

# The log is truncated BEFORE starting: otherwise the `grep | tail -1` below
# would return the URL of a previous run and the .url-publica file
# would point to a tunnel that no longer exists.
: > "$LOG"

if [ ! -x "$BIN" ]; then
  ARCH=$(uname -m)
  case $ARCH in x86_64) A=amd64;; aarch64) A=arm64;; *) A=amd64;; esac
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A" -o "$BIN" \
    || { echo "cloudflared could not be downloaded"; exit 1; }
  chmod +x "$BIN"
fi

"$BIN" tunnel --url http://127.0.0.1:8099 --no-autoupdate >> "$LOG" 2>&1 &
TUN_PID=$!

# Waits for cloudflared to announce the public URL.
for _ in $(seq 1 40); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1)
  if [ -n "$URL" ]; then
    echo "$URL" > "$OUT"
    echo "Public URL: $URL"
    break
  fi
  sleep 1
done

wait $TUN_PID
