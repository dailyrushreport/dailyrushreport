#!/usr/bin/env bash
# Rebuild Daily Rush Report. Safe to run from cron as often as you like.
set -euo pipefail
cd "$(dirname "$0")"
python3 build.py >> build.log 2>&1 || echo "$(date) build failed - previous index.html kept" >> build.log
