#!/bin/bash
# One-command start (macOS / Linux): creates a virtualenv, installs requirements, starts the app.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env: add your SARVAM_API_KEY to it (or run with SARVAM_MOCK=1 for the offline demo)."
fi
PORT="${PORT:-8000}"
(sleep 2; (command -v open >/dev/null && open "http://localhost:$PORT") || (command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT") || true) >/dev/null 2>&1 &
exec .venv/bin/python server.py
