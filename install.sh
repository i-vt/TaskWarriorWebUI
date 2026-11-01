#!/usr/bin/env bash
# TaskWarrior WebUI installer for Debian 12/13 (root).
#
# Usage:
#   ./install.sh              build & start (idempotent)
#   ./install.sh --reset --yes  FULL reset: stop, delete docker volumes,
#                               delete all user data, then reinstall fresh
#   ./install.sh --yes        non-interactive (assume "yes" to prompts)
#
# Everything is logged to logs/install-<timestamp>.log
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RESET=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --reset) RESET=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

mkdir -p logs
LOG_FILE="logs/install-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; exit 1; }
trap 'die "command failed at line $LINENO — see $LOG_FILE"' ERR

confirm() {
    local prompt="$1"
    if [ "$ASSUME_YES" = "1" ]; then return 0; fi
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

env_val() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2-; }

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        die "docker compose plugin not found even after installation attempt"
    fi
}

# ---------------------------------------------------------------------------
# 1. Preflight: root, docker, compose, curl — install anything missing.
# ---------------------------------------------------------------------------
preflight() {
    log "==> Preflight checks"
    [ "$(id -u)" = "0" ] || die "must be run as root"

    if ! command -v docker >/dev/null 2>&1; then
        log "docker not found — installing docker.io"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    fi
    systemctl enable docker >/dev/null 2>&1 || true

    if ! docker compose version >/dev/null 2>&1; then
        log "docker compose plugin not found — installing"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin \
            || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose \
            || die "could not install docker compose"
    fi

    if ! command -v curl >/dev/null 2>&1; then
        log "curl not found — installing"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates
    fi

    log "    docker: $(docker --version)"
    if docker compose version >/dev/null 2>&1; then
        log "    compose: $(docker compose version --short)"
    else
        log "    compose: $(docker-compose version --short)"
    fi
}

# ---------------------------------------------------------------------------
# 2. Environment file
# ---------------------------------------------------------------------------
prepare_env() {
    log "==> Environment file"
    if [ ! -f .env ]; then
        cp .env.example .env
        chmod 600 .env
        log "    created .env from .env.example"
    else
        log "    .env already present (kept as-is)"
    fi
    WEB_PORT="$(env_val WEB_PORT)"; WEB_PORT="${WEB_PORT:-62304}"
    DOMAIN="$(env_val DOMAIN)";   DOMAIN="${DOMAIN// /}"
    DATA_DIR="$(env_val DATA_DIR)"; DATA_DIR="${DATA_DIR:-./data}"
    ACME_EMAIL="$(env_val ACME_EMAIL)"
    if ! [[ "$WEB_PORT" =~ ^[0-9]+$ ]]; then
        die "WEB_PORT must be a number (got: $WEB_PORT)"
    fi
    if [ -n "$DOMAIN" ] && [ -z "$ACME_EMAIL" ]; then
        log "    WARNING: DOMAIN is set but ACME_EMAIL is empty — Let's Encrypt will not be able to e-mail you about expiry"
    fi
}

# ---------------------------------------------------------------------------
# 3. Reset: full wipe including volumes and all user data
# ---------------------------------------------------------------------------
do_reset() {
    log "==> RESET requested"
    confirm "This will STOP the stack, DELETE all docker volumes and ALL user data. Continue?" \
        || die "reset aborted"
    compose down -v --remove-orphans || true
    # Belt and braces: also drop any orphaned volumes/images of this project.
    docker volume ls -q | grep -E '^taskwarrior-webui' | xargs -r docker volume rm || true
    if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        log "    deleted user data at $DATA_DIR"
    fi
    rm -f deploy/.boot-strapped
    log "    reset complete — reinstalling fresh"
}

# ---------------------------------------------------------------------------
# 4. Boot recovery: systemd unit so the stack comes back after a reboot
# ---------------------------------------------------------------------------
install_boot_recovery() {
    log "==> Boot recovery (self-heal after reboot)"
    if ! command -v systemctl >/dev/null 2>&1; then
        log "    no systemd detected — relying on docker restart policies only"
        return 0
    fi
    if [ ! -w /etc/systemd/system ]; then
        log "    /etc/systemd/system not writable — relying on docker restart policies only"
        return 0
    fi
    cat > /etc/systemd/system/taskwarrior-webui.service <<EOF
[Unit]
Description=TaskWarrior WebUI (docker compose stack)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/deploy/boot-start.sh
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable taskwarrior-webui.service >/dev/null
    log "    systemd unit installed and enabled (restart policies also set)"
}

# ---------------------------------------------------------------------------
# 5. Build & start
# ---------------------------------------------------------------------------
deploy() {
    log "==> Building and starting"
    mkdir -p "$DATA_DIR" "$DATA_DIR/app" "$DATA_DIR/users" logs

    compose build

    local args=(up -d --remove-orphans)
    if [ -n "$DOMAIN" ]; then
        args+=(--profile https)
        log "    DOMAIN=$DOMAIN → starting HTTPS (Caddy) profile"
    else
        log "    no DOMAIN set → plain HTTP on port $WEB_PORT"
    fi
    compose "${args[@]}"

    log "==> Waiting for the web UI to become healthy"
    local ok=0
    for i in $(seq 1 60); do
        if curl -fsS "http://127.0.0.1:${WEB_PORT}/api/healthz" 2>/dev/null | grep -q '"ok": true'; then
            ok=1; break
        fi
        sleep 2
    done
    [ "$ok" = "1" ] || die "web UI did not become healthy within 120s (check: compose logs web)"
}

summary() {
    log "==> Installation complete"
    echo
    echo "  Web UI:   http://localhost:${WEB_PORT}"
    [ -n "$DOMAIN" ] && echo "  Web UI:   https://${DOMAIN} (HTTPS via Caddy)"
    echo "  Logs:     $LOG_FILE and the system log in ${DATA_DIR}/app/system.log"
    echo
    echo "  One-time ADMIN registration link(s):"
    docker logs taskwarrior-webui 2>&1 | grep "admin registration link" || true
    echo
    echo "  After registering the admin, open the Admin panel to invite members"
    echo "  and to manage users (disable / reset password / delete data)."
    if [ -z "$(env_val TELEGRAM_BOT_TOKEN)" ]; then
        echo
        echo "  Telegram bot is disabled — set TELEGRAM_BOT_TOKEN in .env to enable."
    fi
}

preflight
prepare_env
[ "$RESET" = "1" ] && do_reset
install_boot_recovery
deploy
summary
