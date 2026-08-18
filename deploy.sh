#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
REMOTE_DIR="${REMOTE_DIR:-/tmp/hls-video-downloader-deploy}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! "$REMOTE_DIR" =~ ^/(tmp|var/tmp)/[A-Za-z0-9._/-]+$ || "$REMOTE_DIR" == *".."* ]]; then
    echo "Error: REMOTE_DIR must be a dedicated directory under /tmp or /var/tmp."
    exit 1
fi

for command_name in rsync ssh; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Error: $command_name is required on this computer."
        exit 1
    fi
done

echo "Deploying to $PI_HOST..."

rsync -az --delete \
    --exclude=.git \
    --exclude=.idea \
    --exclude=.vscode \
    --exclude=.DS_Store \
    --exclude=__pycache__ \
    "$SOURCE_DIR/" "$PI_HOST:$REMOTE_DIR/"

# Allocate a terminal so sudo can request a password when the Pi is not
# configured for passwordless sudo.
ssh -t "$PI_HOST" \
    "cd '$REMOTE_DIR' && chmod +x apply-update.sh && sudo ./apply-update.sh"

echo "Deployment complete."
