# install.ps1 — Windows install script for The Homie
$ErrorActionPreference = "Stop"

function Invoke-NpmInstall {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    Push-Location $Path
    try {
        npm install
        Assert-LastExitCode "npm install in $Path"
    } finally {
        Pop-Location
    }
}

function Invoke-NpmScript {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Script
    )

    Push-Location $Path
    try {
        npm run $Script
        Assert-LastExitCode "npm run $Script in $Path"
    } finally {
        Pop-Location
    }
}

function Assert-LastExitCode {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Action
    )

    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE"
    }
}

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

# Check Git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git not found. Install from https://git-scm.com/download/win"
    exit 1
}
Write-Host "Git OK" -ForegroundColor Green

# Check Node.js 22.12+ for the dashboard and desktop dev stack
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Error "Node.js not found. Install Node.js 22.12+ from https://nodejs.org/"
    exit 1
}
$nodeVersionRaw = & node -p "process.versions.node"
$nodeVersionParts = $nodeVersionRaw.Split('.')
$nodeMajor = [int]$nodeVersionParts[0]
$nodeMinor = [int]$nodeVersionParts[1]
if (($nodeMajor -lt 22) -or (($nodeMajor -eq 22) -and ($nodeMinor -lt 12))) {
    Write-Error "Node.js $nodeVersionRaw found - need 22.12+."
    exit 1
}
Write-Host "Node.js $nodeVersionRaw OK" -ForegroundColor Green

# Install uv if missing
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}

# Clone or use existing
$repoDir = if ($env:THEHOMIE_DIR) { $env:THEHOMIE_DIR } else { "$HOME\thehomie" }
if (-not (Test-Path $repoDir)) {
    git clone https://github.com/TheSmokeDev/taskchad-os.git $repoDir
    Assert-LastExitCode "git clone"
}

# Install deps
Push-Location "$repoDir\.claude\scripts"
uv sync
Assert-LastExitCode "uv sync"

# Create starter .env only when a public example exists. Use the setup wizard
# for provider configuration instead of asking operators to hand-edit secrets.
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example"
    } else {
        "# The Homie configuration" | Out-File -FilePath ".env"
    }
}

# Verify
uv run thehomie setup --check
Assert-LastExitCode "thehomie setup --check"
Pop-Location

# Install dashboard and desktop dependencies. Electron loads the dashboard as
# the product surface; Python and Hono remain the runtime source of truth.
Write-Host "`nInstalling dashboard and desktop dependencies..." -ForegroundColor Cyan
Invoke-NpmInstall "$repoDir\dashboard\server"
Invoke-NpmInstall "$repoDir\dashboard\web"
Invoke-NpmInstall "$repoDir\dashboard\desktop"

Write-Host "`nBuilding dashboard web assets for Desktop v0..." -ForegroundColor Cyan
Invoke-NpmScript "$repoDir\dashboard\web" "build"

Push-Location "$repoDir\.claude\scripts"
try {
    $desktopDryRun = uv run thehomie desktop --shell --dry-run --json
    Assert-LastExitCode "desktop shell dry-run"
    $desktopDryRun | Out-Null
} finally {
    Pop-Location
}

Write-Host "`nInstalled successfully." -ForegroundColor Green
Write-Host "  If setup reported missing providers or chat adapters, finish onboarding first:"
Write-Host "    cd $repoDir\.claude\scripts; uv run thehomie setup"
Write-Host "  Verify:    cd $repoDir\.claude\scripts; uv run thehomie setup --check"
Write-Host "  Chat:      cd $repoDir\.claude\scripts; uv run thehomie chat"
Write-Host "  Desktop:   cd $repoDir\.claude\scripts; uv run thehomie desktop --shell"
Write-Host "  Dev mode:  cd $repoDir\.claude\scripts; uv run thehomie desktop"
Write-Host "  Package:   cd $repoDir\dashboard\desktop; npm run package:win"
