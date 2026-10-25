#!/usr/bin/env sh
# Linux: ./scripts/korsik.sh <команда CLI>
cd "$(dirname "$0")/.." && docker compose exec app python -m korsik.cli "$@"
