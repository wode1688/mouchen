#!/usr/bin/env sh
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_command=${PYTHON:-python3}
if [ ! -x "$repo_root/.venv/bin/python" ]; then
    "$python_command" -m venv "$repo_root/.venv"
fi
requirements=requirements-dev.txt
if [ "${1:-}" = "--runtime-only" ]; then
    requirements=requirements.txt
fi
"$repo_root/.venv/bin/python" -m pip install -r "$repo_root/$requirements"
printf '%s\n' 'Local environment ready. Use .venv/bin/python from this checkout.'
