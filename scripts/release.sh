#!/usr/bin/env bash
# release.sh — bump version, commit, and (on the public repo) tag + push +
# create a real (non-prerelease) GitHub release in one step.
#
# Usage:
#   bash scripts/release.sh 1.1.0
#
# Run from thehomie (private source): bumps pyproject.toml + VERSION-of-
# truth and commits the bump. Then re-export (python scripts/sanitize.py) and
# re-run this same script from thehomie-framework to actually tag/push/release.
#
# Run from thehomie-framework (origin == TheSmokeDev/taskchad-os): also tags
# vX.Y.Z, pushes the commit + tag, and runs `gh release create` as a real
# (non-prerelease) release so the CLI update-checker picks it up immediately.
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: bash scripts/release.sh <version>   (e.g. 1.1.0)"
    exit 1
fi

VERSION="$1"
PYPROJECT="./.claude/scripts/pyproject.toml"

if [ ! -f "$PYPROJECT" ]; then
    echo "ERROR: $PYPROJECT not found — run this from the repo root."
    exit 1
fi

sed -i.bak -E "s/^version = \"[0-9]+\.[0-9]+\.[0-9]+\"/version = \"$VERSION\"/" "$PYPROJECT"
rm -f "${PYPROJECT}.bak"

git add "$PYPROJECT"
git commit -m "chore(release): v$VERSION"

ORIGIN_URL=$(git remote get-url origin 2>/dev/null || echo "")
if [[ "$ORIGIN_URL" == *"TheSmokeDev/taskchad-os"* ]]; then
    echo "Public repo detected — tagging and publishing v$VERSION..."
    git tag "v$VERSION"
    git push origin HEAD
    git push origin "v$VERSION"
    gh release create "v$VERSION" \
        --title "taskchad-os v$VERSION" \
        --generate-notes
    echo "Released v$VERSION — the CLI update-checker will pick it up within 24h (or immediately via 'thehomie update')."
else
    echo "Committed the version bump to v$VERSION."
    echo "This isn't the public taskchad-os repo — next steps:"
    echo "  1. python scripts/sanitize.py"
    echo "  2. cd ../thehomie-framework && bash scripts/release.sh $VERSION"
fi
