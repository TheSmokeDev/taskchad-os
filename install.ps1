# install.ps1 — Windows install script for The Homie
$ErrorActionPreference = "Stop"

# Check Python 3.12+
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found. Install from https://www.python.org/downloads/"
    exit 1
}
$version = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $version.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 12)) {
    Write-Error "Python $version found — need 3.12+."
    exit 1
}
Write-Host "Python $version OK" -ForegroundColor Green

# Install uv if missing
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}

# Clone or use existing
$repoDir = if ($env:THEHOMIE_DIR) { $env:THEHOMIE_DIR } else { "$HOME\thehomie" }
if (-not (Test-Path $repoDir)) {
    git clone https://github.com/thehomie-framework/thehomie.git $repoDir
}

# Install deps
Push-Location "$repoDir\.claude\scripts"
uv sync

# Create .env
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example — edit with your API keys"
    } else {
        "# The Homie configuration" | Out-File -FilePath ".env"
    }
}

# Verify
uv run thehomie setup --check
Pop-Location

Write-Host "`nInstallation complete!" -ForegroundColor Green
Write-Host "  Edit: $repoDir\.claude\scripts\.env"
Write-Host "  Run:  cd $repoDir\.claude\scripts; uv run thehomie chat"
