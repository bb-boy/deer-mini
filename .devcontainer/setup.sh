#!/usr/bin/env bash
set -euo pipefail

echo "Setting up deer-mini backend (Python 3.12)..."
python3.12 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt

echo "Setting up deer-mini frontend (pnpm 10.30.3)..."
npm install --global pnpm@10.30.3
(
  cd frontend
  pnpm install --frozen-lockfile
)

echo "Setup finished. See docs/github-free-testing.md for startup commands."
