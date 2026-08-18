#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/hls-video-downloader"
STATE_DIR="/var/tmp/hls-video-downloader"
SERVICE_NAME="hls-video-downloader"
SERVICE_USER="${SERVICE_USER:-pi}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEGACY_APP_DIR="/opt/lan-hls-video-downloader"
LEGACY_STATE_DIR="/var/tmp/lan-hls-video-downloader"
LEGACY_SERVICE_NAME="lan-hls-video-downloader"
LEGACY_SERVICE_UNIT="/etc/systemd/system/${LEGACY_SERVICE_NAME}.service"
LEGACY_SERVICE_PRESENT=0
LEGACY_SERVICE_STOPPED=0
INSTALL_COMPLETE=0

restore_legacy_service() {
    local exit_code=$?
    set +e

    if [[ $LEGACY_SERVICE_STOPPED -eq 1 && $INSTALL_COMPLETE -eq 0 ]]; then
        echo "Installation failed; attempting to restore the legacy service..." >&2
        systemctl start "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    exit "$exit_code"
}
trap restore_legacy_service EXIT

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo: sudo ./install.sh"
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service user '$SERVICE_USER' does not exist."
    echo "Run with e.g. sudo SERVICE_USER=myuser ./install.sh"
    exit 1
fi

if systemctl cat "$LEGACY_SERVICE_NAME" >/dev/null 2>&1; then
    LEGACY_SERVICE_PRESENT=1
fi

apt-get update
apt-get install -y python3-venv ffmpeg

mkdir -p "$APP_DIR/templates"
cp "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
cp "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SOURCE_DIR/templates/index.html" "$APP_DIR/templates/index.html"
cp "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
cp "$SOURCE_DIR/LICENSE" "$APP_DIR/LICENSE"
cp "$SOURCE_DIR/hls-video-downloader.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# If a non-pi service user was requested, update the unit before installation.
if [[ "$SERVICE_USER" != "pi" ]]; then
    sed -i "s/^User=pi$/User=${SERVICE_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
    sed -i "s/^Group=pi$/Group=${SERVICE_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$STATE_DIR/home"
mkdir -p "$STATE_DIR/tmp"
mkdir -p "$STATE_DIR/jobs"
mkdir -p "$STATE_DIR/browsers"
chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"

# Install Playwright's Chromium dependencies as root, then install the
# browser itself into the shared service-owned browser directory.
PLAYWRIGHT_BROWSERS_PATH="$STATE_DIR/browsers" \
    "$APP_DIR/.venv/bin/playwright" install-deps chromium

sudo -u "$SERVICE_USER" env \
    PLAYWRIGHT_BROWSERS_PATH="$STATE_DIR/browsers" \
    "$APP_DIR/.venv/bin/playwright" install chromium

if [[ ! -d /mnt/Videos ]]; then
    echo
    echo "WARNING: /mnt/Videos does not currently exist."
    echo "Mount the NAS share there before starting the service."
fi

if [[ $LEGACY_SERVICE_PRESENT -eq 1 ]] && \
    systemctl is-active --quiet "$LEGACY_SERVICE_NAME"; then
    echo "Stopping legacy ${LEGACY_SERVICE_NAME} service..."
    systemctl stop "$LEGACY_SERVICE_NAME"
    LEGACY_SERVICE_STOPPED=1
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

if [[ -d /mnt/Videos ]]; then
    if sudo -u "$SERVICE_USER" test -w /mnt/Videos; then
        systemctl restart "$SERVICE_NAME"
        INSTALL_COMPLETE=1

        if [[ $LEGACY_SERVICE_PRESENT -eq 1 ]]; then
            echo "Removing the legacy ${LEGACY_SERVICE_NAME} installation..."
            systemctl disable "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
            rm -f "$LEGACY_SERVICE_UNIT"
            rm -rf -- "$LEGACY_APP_DIR" "$LEGACY_STATE_DIR"
            systemctl daemon-reload
            LEGACY_SERVICE_STOPPED=0
        fi

        echo
        echo "Installed and started ${SERVICE_NAME}."
        echo "Open: http://<PI-IP>:99"
    else
        echo
        echo "Installed but NOT started: ${SERVICE_USER} cannot write to /mnt/Videos."
        echo "Fix the NAS mount permissions, then run:"
        echo "  sudo systemctl start ${SERVICE_NAME}"
    fi
else
    echo
    echo "Installed but NOT started because /mnt/Videos is not mounted."
fi
