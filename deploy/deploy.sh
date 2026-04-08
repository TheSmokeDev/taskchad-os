#!/usr/bin/env bash
# Deploy The Homie bot to a remote server.
# Usage: ./deploy/deploy.sh [user@host] [install_dir]

set -euo pipefail

REMOTE="${1:-root@your-server}"
INSTALL_DIR="${2:-/opt/thehomie}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

echo "Deploying to $REMOTE:$INSTALL_DIR"

# 1. Backup existing on remote
ssh "$REMOTE" "cp -r $INSTALL_DIR $INSTALL_DIR-backup-$TIMESTAMP 2>/dev/null || true"

# 2. Sync code (exclude .env, data, .git)
rsync -avz --exclude='.env' --exclude='data/' --exclude='.git' \
    --exclude='*.pyc' --exclude='__pycache__' --exclude='bot.log' \
    .claude/chat/ "$REMOTE:$INSTALL_DIR/.claude/chat/"
rsync -avz --exclude='.env' --exclude='*.pyc' --exclude='__pycache__' \
    .claude/scripts/ "$REMOTE:$INSTALL_DIR/.claude/scripts/"

# 3. Restore .env from backup
ssh "$REMOTE" "cp $INSTALL_DIR/production.env.backup $INSTALL_DIR/.claude/scripts/.env 2>/dev/null || true"

# 4. Install dependencies + restart
ssh "$REMOTE" "cd $INSTALL_DIR/.claude/scripts && \
    uv sync --no-dev && \
    sudo systemctl restart thehomie"

# 5. Verify
sleep 5
ssh "$REMOTE" "curl -s http://localhost:8787/health | python3 -m json.tool"

echo "Deploy complete."
