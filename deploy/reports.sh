#!/usr/bin/env bash
# Run from a server checkout with core/.env and persistent core/data configured.
set -euo pipefail
cd "$(dirname "$0")/.."
unset COMPOSE_PROFILES COMPOSE_FILE
AI_MANAGER_UID=$(id -u)
AI_MANAGER_GID=$(id -g)
export AI_MANAGER_UID AI_MANAGER_GID
mkdir -p core/data/manager secrets/apns "$HOME/.cache/ki"
compose=(docker compose -f docker-compose.reports.yml)
"${compose[@]}" config --quiet
"${compose[@]}" build report-worker
"${compose[@]}" up -d --no-deps report-worker
"${compose[@]}" exec -T report-worker ai-manager reports --json
