#!/usr/bin/env bash
# deploy.sh — Push local changes to the VPS and reload Apache.
#
# Usage:
#   ./deploy.sh           — deploy app files only (fast)
#   ./deploy.sh --db      — also upload the statcast DB (~515 MB, slow)
#
# Requirements: ssh key auth set up, or enter password when prompted.

set -e

SERVER="carlos@212.28.189.37"
REMOTE="/var/www/hitters"

DEPLOY_DB=false
for arg in "$@"; do
    [[ "$arg" == "--db" ]] && DEPLOY_DB=true
done

echo "==> Syncing app files..."
rsync -avz --progress \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.claude' \
    --exclude='__pycache__' \
    --exclude='data/' \
    --exclude='*.db' \
    --exclude='*.log' \
    --exclude='*.pdf' \
    --exclude='GUMBO/' \
    --exclude='hitter_dashboard.csv' \
    --exclude='savant_data.csv' \
    --exclude='codes*.csv' \
    --exclude='deploy.sh' \
    --exclude='hitters.conf' \
    --exclude='hitters.wsgi' \
    --exclude='dashboard_app.py' \
    --exclude='tbox.py' \
    --exclude='test_api.py' \
    . "$SERVER:$REMOTE/"

if [ "$DEPLOY_DB" = true ]; then
    echo "==> Uploading Statcast DB (~515 MB, this will take a while)..."
    rsync -avz --progress data/statcast_2025.db "$SERVER:$REMOTE/data/"
fi

echo "==> Reloading Apache..."
ssh "$SERVER" "sudo systemctl reload apache2"

echo "==> Done! https://hitters.datanalytics.pro"
