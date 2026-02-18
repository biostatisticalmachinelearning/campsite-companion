# Quick setup script for Campsite Companion
$ErrorActionPreference = "Stop"

Write-Host "=== Campsite Companion Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check Python version
try {
    python --version | Out-Null
} catch {
    Write-Host "Error: Python 3 is required. Install it from https://www.python.org/" -ForegroundColor Red
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

Write-Host "Activating virtual environment..."
& .venv\Scripts\Activate.ps1

Write-Host "Installing dependencies..."
pip install -e . --quiet

# Check if catalog exists
if (-not (Test-Path "data\catalog_recgov.json")) {
    Write-Host ""
    $reply = Read-Host "Build the park catalog now? This is recommended to enable all features. It will take ~5 minutes. (y/n)"
    if ($reply -match "^[Yy]$") {
        Write-Host "Building catalog..."
        build-catalog
    } else {
        Write-Host "Skipping catalog build. Run 'build-catalog' later to enable the Next Available Date Finder."
    }
} else {
    Write-Host "Park catalog already exists."
}

Write-Host ""
Write-Host "=== Setup complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "To start the web server:"
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "  camping-web"
Write-Host ""
Write-Host "Then open http://localhost:8000 in your browser."
