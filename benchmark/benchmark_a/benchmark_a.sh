#!/usr/bin/env bash
# Run Benchmark A from a YAML file.  The Python helper owns the app lifecycle.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${script_dir}/../../.venv/bin/python" "${script_dir}/benchmark_a.py" "$@"
