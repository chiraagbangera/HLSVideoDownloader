#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/lan-hls-video-downloader"
SERVICE_NAME="lan-hls-video-downloader"
SERVICE_USER="${SERVICE_USER:-pi}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo: sudo ./install.sh"
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service user '$SERVICE_USER' does not exist."
    echo "Run with e.g. sudo SERVICE_USER=myuser ./install.sh"
    exit 1
fi

apt-get update
apt-get install -y python3-venv ffmpeg

mkdir -p "$APP_DIR/templates"
cp "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
cp "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SOURCE_DIR/templates/index.html" "$APP_DIR/templates/index.html"
cp "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
cp "$SOURCE_DIR/lan-hls-video-downloader.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# If a non-pi service user was requested, update the unit before installation.
if [[ "$SERVICE_USER" != "pi" ]]; then
    sed -i "s/^User=pi$/User=${SERVICE_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
    sed -i "s/^Group=pi$/Group=${SERVICE_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p /var/tmp/lan-hls-video-downloader/home
mkdir -p /var/tmp/lan-hls-video-downloader/tmp
mkdir -p /var/tmp/lan-hls-video-downloader/jobs
mkdir -p /var/tmp/lan-hls-video-downloader/browsers
chown -R "$SERVICE_USER:$SERVICE_USER" /var/tmp/lan-hls-video-downloader

# Install Playwright's Chromium dependencies as root, then install the
# browser itself into the shared service-owned browser directory.
PLAYWRIGHT_BROWSERS_PATH=/var/tmp/lan-hls-video-downloader/browsers \
    "$APP_DIR/.venv/bin/playwright" install-deps chromium

sudo -u "$SERVICE_USER" env \
    PLAYWRIGHT_BROWSERS_PATH=/var/tmp/lan-hls-video-downloader/browsers \
    "$APP_DIR/.venv/bin/playwright" install chromium

if [[ ! -d /mnt/Videos ]]; then
    echo
    echo "WARNING: /mnt/Videos does not currently exist."
    echo "Mount the NAS share there before starting the service."
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

if [[ -d /mnt/Videos ]]; then
    if sudo -u "$SERVICE_USER" test -w /mnt/Videos; then
        systemctl restart "$SERVICE_NAME"
        echo
        echo "Installed and started ${SERVICE_NAME}."
        echo "Open: http://<PI-IP>:101"
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
