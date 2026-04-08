#!/usr/bin/env bash
# install.sh — Linux/macOS install script for The Homie
set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

run_cmd() {
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] $*"
    else
        "$@"
    fi
}

# Check Python 3.12+
python_cmd=""
for cmd in python3.12 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            python_cmd="$cmd"
            break
        fi
    fi
done
if [ -z "$python_cmd" ]; then
    echo "ERROR: Python 3.12+ required. Install from https://www.python.org/downloads/"
    exit 1
fi
echo "Python $version OK ($python_cmd)"

# Install uv if missing
if ! command -v uv &>/dev/null; then
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
        echo "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="$HOME/.local/bin:$PATH"
fi

# Clone or use existing repo
REPO_DIR="${THEHOMIE_DIR:-$HOME/thehomie}"
if [ ! -d "$REPO_DIR" ]; then
    run_cmd git clone https://github.com/thehomie-framework/thehomie.git "$REPO_DIR"
fi

# Install dependencies
if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] cd $REPO_DIR/.claude/scripts && uv sync"
else
    cd "$REPO_DIR/.claude/scripts"
    uv sync
fi

# Create .env from example if missing
if [ ! -f .env ] || [ "$DRY_RUN" = true ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] Create .env from .env.example (or empty)"
    elif [ -f .env.example ]; then
        cp .env.example .env
        echo "Created .env from .env.example — edit with your API keys"
    else
        echo "# The Homie configuration" > .env
        echo "Created empty .env — add your API keys"
    fi
fi

# Verify
if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] uv run thehomie setup --check"
    echo ""
    echo "Dry run complete. To install for real: bash install.sh"
else
    uv run thehomie setup --check
    echo ""
    echo "Installation complete!"
    echo "  Edit: $REPO_DIR/.claude/scripts/.env"
    echo "  Run:  cd $REPO_DIR/.claude/scripts && uv run thehomie chat"
fi
