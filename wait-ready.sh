#!/bin/sh
# Wait until Screenpilot is really up: the page answers, the desktop runs and its picture arrives.
# Says so when it is, and says what is wrong when it is not. Used by run.sh; run it yourself any time.
# Usage: ./wait-ready.sh [url] [seconds]
URL="${1:-http://localhost:8080}"
LIMIT="${2:-180}"
waited=0; health=""
while [ "$waited" -lt "$LIMIT" ]; do
    health=$(curl -fsS --max-time 3 "$URL/api/health" 2>/dev/null) || health=""
    case "$health" in
    *'"ok": true'*|*'"ok":true'*)
        echo ""; echo "Screenpilot is ready: $URL"; exit 0;;
    esac
    waited=$((waited + 2)); sleep 2
done
echo ""
echo "Screenpilot did not come up within $LIMIT seconds."
if [ -n "$health" ]; then
    echo "It answers, but reports a problem:"
    echo "  $health" | sed 's/.*"problems": *\[\([^]]*\)\].*/  \1/'
else
    echo "The page at $URL does not answer."
fi
echo "See why with:  docker compose ps   and   docker compose logs --tail 80"
exit 1
