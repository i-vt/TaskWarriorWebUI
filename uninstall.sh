#!/usr/bin/env bash
# TaskWarrior WebUI uninstaller.
#
# Usage:
#   ./uninstall.sh            stop & remove containers, networks and volumes
#                             (keeps .env and user data)
#   ./uninstall.sh --purge    also delete ALL user data, .env and images
#   ./uninstall.sh --yes      non-interactive
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

log() { echo "[uninstall] $*"; }

confirm() {
    [ "$ASSUME_YES" = "1" ] && return 0
    read -r -p "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

env_val() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2-; }

compose() {
    if docker compose version >/dev/null 2>&1; then docker compose "$@";
    elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"; fi
}

DATA_DIR="$(env_val DATA_DIR)"; DATA_DIR="${DATA_DIR:-./data}"

log "Stopping and removing containers, networks and volumes"
compose down -v --remove-orphans 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files | grep -q '^taskwarrior-webui.service'; then
        systemctl disable taskwarrior-webui.service >/dev/null 2>&1 || true
        rm -f /etc/systemd/system/taskwarrior-webui.service
        systemctl daemon-reload
        log "Removed systemd boot-recovery unit"
    fi
fi

if [ "$PURGE" = "1" ]; then
    confirm "Also delete ALL user data in $DATA_DIR and the .env file?" || true
    [ -d "$DATA_DIR" ] && rm -rf "$DATA_DIR" && log "Deleted $DATA_DIR"
    [ -f .env ] && rm -f .env && log "Deleted .env"
    if compose images >/dev/null 2>&1; then
        compose images -q | xargs -r docker rmi || true
        docker image rm caddy:2 2>/dev/null || true
        log "Removed project images"
    fi
else
    log "Kept user data ($DATA_DIR) and .env — re-run install.sh to redeploy"
    log "Use --purge to remove them as well"
fi

log "Done"
