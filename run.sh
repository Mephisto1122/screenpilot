#!/bin/sh
# Start Screenpilot with native Ollama, so a Mac runs inference on its Apple GPU.
# Usage: ./run.sh            (extra args go to `docker compose up`, e.g. ./run.sh -d)
set -eu
cd "$(dirname "$0")"

MODEL="${OLLAMA_MODEL:-}"
if [ -z "$MODEL" ] && [ -f .env ]; then
    MODEL=$(sed -n 's/^OLLAMA_MODEL=//p' .env | tail -n 1 | tr -d '"'"'"' ')
fi
if [ -z "$MODEL" ]; then
    # Qwen3-VL runs fully on this Mac. 8B reads and clicks better but needs about 7 GB next to
    # Docker (which usually takes half the memory) and macOS: only Macs with 24 GB or more get it.
    # With 16 GB the 8B model makes the whole Mac swap, and every step takes minutes.
    MEM=$(sysctl -n hw.memsize 2>/dev/null || awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "${MEM:-0}" -ge 23000000000 ]; then MODEL=qwen3-vl:8b; else MODEL=qwen3-vl:4b; fi
fi
export OLLAMA_MODEL="$MODEL"
OLLAMA="http://localhost:11434"
# Set OLLAMA_MODEL=none to skip the Qwen3-VL download — for when you only use UI-TARS or the cloud models.
SKIP_LOCAL=0
case "$MODEL" in none|off|"") SKIP_LOCAL=1; export OLLAMA_MODEL=none;; esac

ollama_up() { curl -fsS --max-time 2 "$OLLAMA/api/tags" >/dev/null 2>&1; }

command -v docker >/dev/null 2>&1 || { echo "Docker Desktop is required: https://www.docker.com/products/docker-desktop/" >&2; exit 1; }

if ! ollama_up; then
    if [ "$(uname -s)" = "Darwin" ] && open -Ra Ollama >/dev/null 2>&1; then
        echo "Starting the Ollama app..."
        open -ga Ollama
    elif command -v ollama >/dev/null 2>&1; then
        echo "Starting ollama serve..."
        nohup ollama serve >/tmp/screenpilot-ollama.log 2>&1 &
    else
        echo "Ollama is not installed. Get it from https://ollama.com/download, open it once, then rerun ./run.sh" >&2
        echo "(To run Ollama inside Docker instead, CPU-only on a Mac: docker compose -f compose.yaml -f compose.ollama.yaml up --build)" >&2
        exit 1
    fi
    i=0
    until ollama_up; do
        i=$((i + 1)); [ "$i" -ge 30 ] && { echo "Ollama did not start on $OLLAMA" >&2; exit 1; }
        sleep 1
    done
fi
echo "Ollama is running natively on $OLLAMA"

case "$MODEL" in
none) ;;
qwen3-vl*)
    # Qwen3-VL needs Ollama 0.12.7 or newer.
    VERSION=$(curl -fsS --max-time 2 "$OLLAMA/api/version" 2>/dev/null | sed -n 's/.*"version":"\([0-9.]*\).*/\1/p')
    if [ -n "$VERSION" ] && [ "$(printf '%s\n0.12.7\n' "$VERSION" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)" != "0.12.7" ]; then
        echo "Your Ollama is version $VERSION; $MODEL needs 0.12.7 or newer. Update the Ollama app, then run ./run.sh again." >&2
        exit 1
    fi;;
esac
if [ "$SKIP_LOCAL" = "1" ]; then
    echo "Skipping the Qwen3-VL download (OLLAMA_MODEL=none)."
elif command -v ollama >/dev/null 2>&1; then
    echo "Local vision model: $MODEL (screenshots stay on this Mac)"
    if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
        echo "Downloading $MODEL into Ollama (one time, a few GB)..."
        ollama pull "$MODEL"
    fi
fi

# The two models that make "Private" strong and clicking sharp are installed by default on a Mac with room
# for them (24 GB or more). With less memory they are opt-in: UITARS=1 ./run.sh, HOLO=1 ./run.sh.
# Turn either off with UITARS=0 or HOLO=0.
MEM="${MEM:-$(sysctl -n hw.memsize 2>/dev/null || awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0)}"
if [ "${MEM:-0}" -ge 23000000000 ]; then BIG=1; else BIG=0; fi

# UI-TARS: an open model trained for computer control. "Private" mode uses it when it is installed.
# It runs on this Mac; screenshots stay here.
UITARS_MODEL="${UITARS_MODEL:-hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M}"
export UITARS_MODEL
if [ "${UITARS:-$BIG}" = "1" ]; then
    if command -v ollama >/dev/null 2>&1 && ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$UITARS_MODEL"; then
        echo "Downloading UI-TARS ($UITARS_MODEL) from Hugging Face (about 5 GB, one time)..."
        ollama pull "$UITARS_MODEL" || echo "WARNING: could not pull UI-TARS automatically. See 'If UI-TARS misbehaves' in docs/GUIDE.md."
    fi
fi
export SCREENPILOT_PLANNER="${SCREENPILOT_PLANNER:-}"

# Holo: a grounding model that makes clicking much sharper. It is used whenever it is installed.
HOLO_MODEL="${HOLO_MODEL:-ahmadwaqar/holo-3.1:4b}"
export HOLO_MODEL
if [ "${HOLO:-$BIG}" = "1" ]; then
    if command -v ollama >/dev/null 2>&1 && ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$HOLO_MODEL"; then
        echo "Downloading Holo ($HOLO_MODEL) from Ollama (about 6 GB, one time)..."
        ollama pull "$HOLO_MODEL" || echo "WARNING: could not pull Holo automatically."
    fi
fi

# The container uses this Mac's time zone, so the log and the agent's clock show your time.
if [ -z "${TZ:-}" ]; then
    TZ=$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p')
    [ -n "$TZ" ] && export TZ
fi

# Build first, so the wait below measures the start, not a long first build.
docker compose build

# In the background: wait until Screenpilot really is up (page, desktop, picture) and say so, or say what is wrong.
detached=0
for arg in "$@"; do case "$arg" in -d|--detach) detached=1;; esac; done
if [ "$detached" = "1" ]; then
    docker compose up --remove-orphans "$@"
    exec ./wait-ready.sh
fi
./wait-ready.sh &
exec docker compose up --remove-orphans "$@"
