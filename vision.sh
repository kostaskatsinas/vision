#!/usr/bin/env bash
# Start Vision with one command: ./vision.sh
#
# First run: creates the .venv environment and runs setup.py.
# Later runs: reuses .venv, re-runs setup.py only if requirements.txt changed,
# then starts the app. Extra arguments are passed to main.py.
#
#   ./vision.sh --update   pull the latest code from GitHub first
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

if [[ "${1:-}" == "--update" ]]; then
    shift
    echo "▶ Updating Vision from GitHub"
    git pull --ff-only
fi

PYTHON="${PYTHON:-python3}"
VENV=".venv"
STAMP="$VENV/.requirements.sha256"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "▶ Creating the Python environment in $VENV"
    if ! "$PYTHON" -m venv "$VENV"; then
        echo "Could not create the environment. Install venv support with:"
        echo "    sudo apt install python3-venv"
        exit 1
    fi
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

want="$(sha256sum requirements.txt setup.py | sha256sum | cut -d' ' -f1)"
have="$(cat "$STAMP" 2>/dev/null || true)"
if [[ "$want" != "$have" ]]; then
    echo "▶ Installing or updating packages (only when requirements change)"
    python setup.py
    echo "$want" > "$STAMP"
fi

exec python main.py "$@"
