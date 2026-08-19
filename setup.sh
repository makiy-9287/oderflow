#!/usr/bin/env bash
#
# orderflow-signals :: Phase 1 VPS bootstrap
# Target: Ubuntu 22.04 / 24.04 LTS (Debian 12 works with minor tweaks)
#
# Run from the project root as a sudo-capable NON-root user:
#   chmod +x setup.sh && ./setup.sh
#
set -Eeuo pipefail

APP_NAME="ofsignals"
APP_USER="ofsignals"
APP_HOME="/opt/ofsignals"
PY_MIN_MINOR=11
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "failed at line $LINENO"' ERR

[[ $EUID -eq 0 ]] && die "Do not run as root. Use a sudo-capable user; the script escalates only where needed."
sudo -v || die "sudo access required."

# ---------------------------------------------------------------- 1. OS packages
log "Updating apt index and installing base packages…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  build-essential git curl jq tmux ca-certificates \
  python3 python3-venv python3-dev python3-pip \
  chrony ufw logrotate

# ---------------------------------------------------------------- 2. Python version
PY_BIN="$(command -v python3)"
PY_MINOR="$("$PY_BIN" -c 'import sys; print(sys.version_info.minor)')"
if (( PY_MINOR < PY_MIN_MINOR )); then
  warn "python3.$PY_MINOR detected; installing 3.11 from deadsnakes…"
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
  PY_BIN="$(command -v python3.11)"
fi
log "Using interpreter: $PY_BIN ($("$PY_BIN" --version))"

# ---------------------------------------------------------------- 3. Clock discipline
# Binance rejects signed requests whose timestamp drifts beyond recvWindow.
log "Enabling NTP sync (chrony)…"
sudo systemctl enable --now chrony >/dev/null 2>&1 || sudo systemctl enable --now chronyd
sudo timedatectl set-ntp true || true
sudo timedatectl set-timezone UTC
log "Clock: $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC"

# ---------------------------------------------------------------- 4. Firewall (egress-only workload)
log "Configuring ufw (SSH in, everything else out)…"
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null
sudo ufw status verbose | head -n 6

# ---------------------------------------------------------------- 5. Service account + layout
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "Creating system user '$APP_USER'…"
  sudo useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
fi
sudo mkdir -p "$APP_HOME"/{app,data,logs}
log "Syncing source tree to $APP_HOME/app…"
sudo rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
  "$SRC_DIR"/ "$APP_HOME/app"/

# ---------------------------------------------------------------- 6. Virtualenv
log "Building isolated venv…"
sudo -u "$APP_USER" "$PY_BIN" -m venv "$APP_HOME/.venv"
sudo -u "$APP_USER" "$APP_HOME/.venv/bin/pip" install --upgrade -q pip wheel setuptools
sudo -u "$APP_USER" "$APP_HOME/.venv/bin/pip" install -q -r "$APP_HOME/app/requirements.txt"
log "Installed: $(sudo -u "$APP_USER" "$APP_HOME/.venv/bin/pip" list --format=freeze | wc -l) packages"

# ---------------------------------------------------------------- 7. Secrets file
if [[ ! -f "$APP_HOME/.env" ]]; then
  sudo cp "$APP_HOME/app/.env.example" "$APP_HOME/.env"
  warn "Created $APP_HOME/.env from template — EDIT IT BEFORE STARTING."
fi
sudo chown -R "$APP_USER:$APP_USER" "$APP_HOME"
sudo chmod 700 "$APP_HOME"
sudo chmod 600 "$APP_HOME/.env"          # secrets are never world-readable
sudo chmod 750 "$APP_HOME"/{data,logs}

# ---------------------------------------------------------------- 8. systemd + logrotate
log "Installing systemd unit and logrotate policy…"
sudo install -m 0644 "$APP_HOME/app/deploy/${APP_NAME}.service" "/etc/systemd/system/${APP_NAME}.service"
sudo install -m 0644 "$APP_HOME/app/deploy/logrotate.${APP_NAME}" "/etc/logrotate.d/${APP_NAME}"
sudo systemctl daemon-reload
sudo systemctl enable "${APP_NAME}.service" >/dev/null

cat <<EOF

$(log 'Phase 1 complete.')

  Next steps
  ──────────
  1. Add credentials:      sudo nano $APP_HOME/.env
     • Binance key must be READ-ONLY (no trading, no withdrawal) + IP-whitelisted to this VPS.
  2. Smoke-test the stack:  sudo -u $APP_USER $APP_HOME/.venv/bin/python -m ofsignals.tools.preflight
     (run from $APP_HOME/app, or export PYTHONPATH=$APP_HOME/app/src)
  3. Start the daemon:      sudo systemctl start ${APP_NAME}
  4. Watch it live:         journalctl -u ${APP_NAME} -f -o cat

EOF
