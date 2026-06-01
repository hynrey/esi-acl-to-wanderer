#!/usr/bin/env bash
#
# Cron-pull deploy: fetch the latest main, and if it changed, rebuild and restart
# the compose service. Safe to run on a tight cron interval — it no-ops when there's
# nothing new and refuses to overlap with itself (flock).
#
# Install (on the server):
#   git clone https://github.com/hynrey/esi-acl-to-wanderer.git /opt/esi-acl-to-wanderer
#   cd /opt/esi-acl-to-wanderer && cp .env.example .env && $EDITOR .env   # fill secrets
#   docker compose up -d --build                                          # first boot
#   crontab -e  ->  */2 * * * * /opt/esi-acl-to-wanderer/scripts/deploy.sh >> /var/log/esi-acl-deploy.log 2>&1
#
# Public repo => pulls over HTTPS with no credentials. No SSH key, no GitHub secrets.
set -euo pipefail

# Repo root = the directory that contains this script's parent (scripts/..).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "$REPO_DIR"

# Single-flight: if a previous deploy is still building, skip this tick.
exec 9>".deploy.lock"
if ! flock -n 9; then
  echo "$(date -Is) deploy already running; skipping"
  exit 0
fi

git fetch --quiet origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0  # already up to date — quiet no-op
fi

echo "$(date -Is) new commit ${LOCAL:0:7} -> ${REMOTE:0:7}; deploying"

# Fast-forward only: never rewrite local history on the server. If this fails the
# server has diverged (someone edited files in place) — fix it manually.
git pull --ff-only origin "$BRANCH"

# Rebuild the image and restart. state.json + config.yaml live on volumes, so the
# refresh token, ETag cache and managed set survive the restart.
docker compose up -d --build

# Reclaim space from the previous image layers.
docker image prune -f >/dev/null 2>&1 || true

echo "$(date -Is) deployed $(git rev-parse --short HEAD)"
