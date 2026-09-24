#!/bin/bash
set -u
cd /opt/skillgo
SVC="${1:-worker-1}"
LINES="${2:-80}"
docker compose logs --since 30m --tail "$LINES" "$SVC" 2>&1
