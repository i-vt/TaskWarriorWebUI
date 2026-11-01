#!/usr/bin/env bash
# Starts (or recovers) the TaskWarrior WebUI stack after a host reboot.
# Invoked by the systemd unit installed by install.sh; safe to run by hand.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    echo "[boot-start] .env missing — nothing to start" >&2
    exit 1
fi

DOMAIN_VALUE="$(grep -E '^DOMAIN=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]')"

ARGS=(up -d)
if [ -n "$DOMAIN_VALUE" ]; then
    ARGS+=(--profile https)
fi

echo "[boot-start] docker compose ${ARGS[*]}"
exec docker compose "${ARGS[@]}"
