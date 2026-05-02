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

# Create venv if missing
if (-not (Test-Path ".\venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
}

# Activate venv
. .\venv\Scripts\Activate.ps1

# Install / refresh dependencies
Write-Host "Checking dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet

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
