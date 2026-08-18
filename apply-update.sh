#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/hls-video-downloader"
SERVICE_NAME="hls-video-downloader"
SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREPARED_UNIT=""
SERVICE_STOPPED=0
UPDATE_COMPLETE=0

wait_for_service() {
    local attempt

    for ((attempt = 1; attempt <= 15; attempt++)); do
        if systemctl is-active --quiet "$SERVICE_NAME" && \
            "$APP_DIR/.venv/bin/python" -c \
                'import urllib.request; urllib.request.urlopen("http://127.0.0.1:99/api/health", timeout=2).read()' \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    return 1
}

cleanup() {
    local exit_code=$?
    set +e

    if [[ -n "$PREPARED_UNIT" ]]; then
        rm -f "$PREPARED_UNIT"
    fi

    # Do not leave the application stopped if an update step fails.
    if [[ $SERVICE_STOPPED -eq 1 && $UPDATE_COMPLETE -eq 0 ]]; then
        echo "Update failed; attempting to start the service again..." >&2
        systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    exit "$exit_code"
}
trap cleanup EXIT

if [[ $EUID -ne 0 ]]; then
    echo "Run this updater with sudo: sudo ./apply-update.sh"
    exit 1
fi

for required_file in \
    app.py \
    requirements.txt \
    README.md \
    LICENSE \
    browser-capture.js \
    install.sh \
    hls-video-downloader.service \
    templates/index.html; do
    if [[ ! -f "$SOURCE_DIR/$required_file" ]]; then
        echo "Missing deployment file: $required_file"
        exit 1
    fi
done

if [[ ! -x "$APP_DIR/.venv/bin/pip" || ! -f "$SERVICE_UNIT" ]]; then
    echo "No complete installation was found; running the initial installer..."
    chmod +x "$SOURCE_DIR/install.sh"
    "$SOURCE_DIR/install.sh"

    if ! wait_for_service; then
        echo "Installation finished, but ${SERVICE_NAME} is not active." >&2
        echo "Check that /mnt/Videos is mounted and writable by the service user." >&2
        systemctl status "$SERVICE_NAME" --no-pager --lines=20 >&2 || true
        journalctl -u "$SERVICE_NAME" --no-pager --lines=40 >&2 || true
        exit 1
    fi

    UPDATE_COMPLETE=1
    echo "Initial installation complete; ${SERVICE_NAME} is active."
    exit 0
fi

echo "Checking the updated Python source..."
"$APP_DIR/.venv/bin/python" -m py_compile "$SOURCE_DIR/app.py"

SERVICE_USER="$(systemctl show "$SERVICE_NAME" --property=User --value)"
if [[ -z "$SERVICE_USER" ]]; then
    SERVICE_USER="pi"
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Configured service user '$SERVICE_USER' does not exist."
    exit 1
fi

SERVICE_GROUP="$(systemctl show "$SERVICE_NAME" --property=Group --value)"
if [[ -z "$SERVICE_GROUP" ]]; then
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
fi

REQUIREMENTS_CHANGED=0
if ! cmp -s "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"; then
    REQUIREMENTS_CHANGED=1
fi

# Preserve a custom service account chosen during the original installation.
PREPARED_UNIT="$(mktemp)"
sed \
    -e "s/^User=pi$/User=${SERVICE_USER}/" \
    -e "s/^Group=pi$/Group=${SERVICE_GROUP}/" \
    "$SOURCE_DIR/hls-video-downloader.service" > "$PREPARED_UNIT"

UNIT_CHANGED=0
if ! cmp -s "$PREPARED_UNIT" "$SERVICE_UNIT"; then
    UNIT_CHANGED=1
fi

echo "Stopping ${SERVICE_NAME}..."
systemctl stop "$SERVICE_NAME"
SERVICE_STOPPED=1

echo "Applying updated application files..."
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
    "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
    "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
    "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
    "$SOURCE_DIR/LICENSE" "$APP_DIR/LICENSE"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
    "$SOURCE_DIR/browser-capture.js" "$APP_DIR/browser-capture.js"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$APP_DIR/templates"
cp -a "$SOURCE_DIR/templates/." "$APP_DIR/templates/"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/templates"

if [[ $REQUIREMENTS_CHANGED -eq 1 ]]; then
    echo "Installing changed Python dependencies..."
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
fi

if [[ $UNIT_CHANGED -eq 1 ]]; then
    echo "Applying updated systemd service..."
    install -o root -g root -m 0644 "$PREPARED_UNIT" "$SERVICE_UNIT"
    systemctl daemon-reload
fi

echo "Restarting ${SERVICE_NAME}..."
systemctl restart "$SERVICE_NAME"

if ! wait_for_service; then
    echo "Service failed its post-deployment health check. Recent status:" >&2
    systemctl status "$SERVICE_NAME" --no-pager --lines=20 >&2 || true
    journalctl -u "$SERVICE_NAME" --no-pager --lines=40 >&2 || true
    exit 1
fi

UPDATE_COMPLETE=1
SERVICE_STOPPED=0
echo "${SERVICE_NAME} is active."
