# RepoDoctor2 — Windows PowerShell Launcher
# Run from the project folder: .\start.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  RepoDoctor2 - Starting up..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Pull latest code
Write-Host "`nPulling latest code from main..." -ForegroundColor Yellow
git pull origin main

# Make sure a real Python is installed (the Windows Store alias is not one)
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd -or $pythonCmd.Source -like "*WindowsApps*") {
    Write-Host "`nERROR: Python is not installed (or only the Windows Store alias was found)." -ForegroundColor Red
    Write-Host "Install Python 3 from https://www.python.org/downloads/ and check" -ForegroundColor Red
    Write-Host "'Add python.exe to PATH' during setup, then run this script again." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

# Create venv if missing
if (-not (Test-Path ".\venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path ".\venv\Scripts\Activate.ps1")) {
        Write-Host "`nERROR: Could not create the virtual environment." -ForegroundColor Red
        Write-Host "Check your Python install, then run this script again." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
}

# Activate venv
. .\venv\Scripts\Activate.ps1

# Install / refresh dependencies
Write-Host "Checking dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nERROR: Dependency install failed (network problem?)." -ForegroundColor Red
    Write-Host "Fix your connection and run .\start.ps1 again." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

# Free port 5001 if something's already on it
$existing = Get-NetTCPConnection -LocalPort 5001 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing instance on port 5001..." -ForegroundColor Yellow
    $existing | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

# Open browser after short delay
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:5001"
} | Out-Null

Write-Host "`nStarting RepoDoctor on http://127.0.0.1:5001" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop the server.`n" -ForegroundColor Green

python app.py
