#!/usr/bin/env bash
# Run the whole stack locally against a fake car drive (no Pi, no gadget, no NAS).
#   dev/run_local.sh            archiver + uploader in the background, web UI on :8765
#   dev/run_local.sh web        web UI only
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements-dev.txt
fi

"$PY" dev/make_sample.py

CONFIG="$ROOT/dev/config.toml"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<EOF
[paths]
archive_dir = "$ROOT/dev/data/archive/events"
db_path = "$ROOT/dev/data/archive/teslacam.db"

[car]
mode = "directory"
source_dir = "$ROOT/dev/data/car"
scan_interval = 20
settle_seconds = 30

[upload]
host = ""                       # local destination instead of the NAS
remote_dir = "$ROOT/dev/data/nas"
bwlimit_kbps = 300              # slow enough to watch the queue move
max_batch_files = 6
poll_interval = 10

[budget]
monthly_gb = 0.05
interface = "none"

[web]
host = "127.0.0.1"
port = 8765
EOF
fi
export TESLACAM_CONFIG="$CONFIG"

"$PY" -m teslacam init-db
if [ "${1:-}" != "web" ]; then
  "$PY" -m teslacam archive-once
  "$PY" -m teslacam archiver & ARCH=$!
  "$PY" -m teslacam uploader & UP=$!
  trap 'kill $ARCH $UP 2>/dev/null' EXIT
fi
"$PY" -m teslacam web
