#!/usr/bin/env bash
set -e

# Run from the backend/ directory
cd "$(dirname "$0")"

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — fill in your API keys before running."
  exit 1
fi

# Install deps if venv doesn't exist
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "Starting Brazilian Business Finder..."
echo "  Web UI: http://localhost:8000"
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
