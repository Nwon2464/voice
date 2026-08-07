#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
log_path="${XDG_RUNTIME_DIR:-/tmp}/interview-assistant.log"

nohup "$app_dir/.venv/bin/python" "$app_dir/interview_app.py" \
    >>"$log_path" 2>&1 </dev/null &

printf 'Interview Assistant started (PID %s)\n' "$!"
printf 'Runtime log: %s\n' "$log_path"
