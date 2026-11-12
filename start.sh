#!/bin/sh
# Start the Screenpilot server. It sets up the agent's computer (its own user, home folder,
# Documents and Downloads in the data volume) and starts each worker's desktop itself.
set -eu
exec python3 -m app.server --host 0.0.0.0 --data /data
