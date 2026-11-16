#!/usr/bin/env bash
# Shows the CURRENT public URL of the logs.
#
# The Cloudflare quick tunnel generates a RANDOM subdomain on every
# start, so the URL may change if the service is restarted.
# This script says which one it is right now.
#
#   probes/url_publica.sh
set -uo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${CXP_LOGS_DIR:-$RAIZ/logs-conversaciones}"
F="$DIR/.url-publica"
if [ -f "$F" ]; then
  cat "$F"
else
  echo "(no URL yet: the tunnel may still be starting)" >&2
  exit 1
fi
