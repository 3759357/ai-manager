#!/usr/bin/env bash
# Run from a server checkout with core/.env and persistent core/data configured.
set -euo pipefail
cd "$(dirname "$0")/.."
unset COMPOSE_PROFILES COMPOSE_FILE
compose=(docker compose -f docker-compose.reports.yml)
"${compose[@]}" config --quiet
"${compose[@]}" build report-worker
"${compose[@]}" up -d --no-deps report-worker
"${compose[@]}" exec -T report-worker ai-manager reports --json
