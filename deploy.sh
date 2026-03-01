#!/usr/bin/env bash
# deploy.sh — Deploy via git: push to origin, pull on VPS, reload Apache.
#
# Usage:
#   ./deploy.sh           — deploy code changes only
#   ./deploy.sh --db      — also upload the statcast DB (~515 MB)

set -e

SERVER="carlos@212.28.189.37"
REMOTE="/var/www/hitters"

DEPLOY_DB=false
for arg in "$@"; do
    [[ "$arg" == "--db" ]] && DEPLOY_DB=true
done

# 1. Commit check — warn if there are uncommitted changes
if ! git diff --quiet HEAD 2>/dev/null; then
    echo "WARNING: You have uncommitted changes. Commit first or they won't deploy."
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
fi

# 2. Push current branch to origin
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Pushing ${BRANCH} to origin..."
git push origin "$BRANCH"

# 3. Pull on VPS and reload
echo "==> Pulling on VPS and reloading Apache..."
ssh "$SERVER" "cd $REMOTE && git fetch origin && git reset --hard origin/$BRANCH && sudo systemctl reload apache2"

# 4. Optionally upload DB (too large for git)
if [ "$DEPLOY_DB" = true ]; then
    echo "==> Uploading Statcast DB (~515 MB)..."
    scp data/statcast_2025.db "$SERVER:$REMOTE/data/"
    echo "    DB uploaded."
fi

echo "==> Done! https://hitters.datanalytics.pro"
