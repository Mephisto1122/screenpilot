#!/bin/sh
# Double-click in Finder: rebuild and restart Screenpilot in the background, then open the dashboard.
cd "$(dirname "$0")" || exit 1
./run.sh -d && sleep 3 && open http://localhost:8080
echo; echo "Screenpilot rebuilt. You can close this window."
