#!/usr/bin/env bash
# deploy.sh — Deploy via git: push to origin, pull on VPS, reload Apache.
#
# Usage:
#   ./deploy.sh           — deploy code changes only
#   ./deploy.sh --db      — also upload the statcast DB (~515 MB)
#
# One-time setup:
#   1. Push this repo to GitHub/GitLab (e.g. git remote add origin ...)
#   2. On the VPS: git clone <repo-url> /var/www/hitters
#   3. On the VPS: create venv at /var/www/hitters/venv, pip install -r requirements_new.txt
#   4. Ensure carlos can: sudo systemctl reload apache2 (NOPASSWD in sudoers)

set -e

SERVER="carlos@212.28.189.37"
REMOTE="/var/www/hitters"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

DEPLOY_DB=false
for arg in "$@"; do
    [[ "$arg" == "--db" ]] && DEPLOY_DB=true
done

# 1. Push current branch to origin
echo "==> Pushing ${BRANCH} to origin..."
git push origin "$BRANCH"

# 2. Pull on VPS and reload
echo "==> Pulling on VPS and reloading Apache..."
ssh "$SERVER" "cd $REMOTE && git pull origin $BRANCH && sudo systemctl reload apache2"

# 3. Optionally upload DB (too large for git)
if [ "$DEPLOY_DB" = true ]; then
    echo "==> Uploading Statcast DB (~515 MB)..."
    scp data/statcast_2025.db "$SERVER:$REMOTE/data/"
    echo "    DB uploaded."
fi

echo "==> Done! https://hitters.datanalytics.pro"
