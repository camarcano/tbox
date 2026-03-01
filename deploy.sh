#!/usr/bin/env bash
# deploy.sh — Push local changes to the VPS and reload Apache.
#
# Usage:
#   ./deploy.sh           — deploy app files only
#   ./deploy.sh --db      — also upload the statcast DB (~515 MB, slow)
#
# Requires: scp + ssh (available in Git Bash by default)

set -e

SERVER="carlos@212.28.189.37"
REMOTE="/var/www/hitters"

DEPLOY_DB=false
for arg in "$@"; do
    [[ "$arg" == "--db" ]] && DEPLOY_DB=true
done

echo "==> Uploading Python files..."
scp app.py hitter_dashboard.py pitcher_dashboard.py build_statcast_db.py player_mapper.py \
    "$SERVER:$REMOTE/"

echo "==> Uploading data files..."
scp requirements_new.txt "SFBB Player ID Map - PLAYERIDMAP.csv" \
    "$SERVER:$REMOTE/"

echo "==> Uploading static files..."
scp static/index.html static/pitcher.html \
    "$SERVER:$REMOTE/static/"
scp static/css/custom.css \
    "$SERVER:$REMOTE/static/css/"
scp static/js/app.js static/js/pitcher-app.js static/js/api-client.js \
    "$SERVER:$REMOTE/static/js/"

if [ "$DEPLOY_DB" = true ]; then
    echo "==> Uploading Statcast DB (~515 MB, this will take a while)..."
    scp data/statcast_2025.db "$SERVER:$REMOTE/data/"
fi

echo "==> Reloading Apache..."
ssh "$SERVER" "sudo systemctl reload apache2"

echo "==> Done! https://hitters.datanalytics.pro"
