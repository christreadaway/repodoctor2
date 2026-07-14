#!/bin/bash
# RepoDoctor — Mac Launcher
# Double-click this file to start RepoDoctor and open the browser.

# Navigate to the project directory
cd "$(dirname "$0")" || { echo "ERROR: Could not find RepoDoctor directory."; read -p "Press Enter to close..."; exit 1; }

echo "========================================"
echo "  RepoDoctor — Starting up..."
echo "========================================"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv || { echo "ERROR: Python 3 not found. Install from python.org or: brew install python@3.12"; read -p "Press Enter to close..."; exit 1; }
fi
source venv/bin/activate

# Install dependencies until they succeed once (stamp file). Gating on the
# venv dir alone meant a failed first-run pip install was never retried —
# every later launch died with ModuleNotFoundError and no guidance.
if [ ! -f "venv/.deps-installed" ]; then
    echo "Installing dependencies..."
    if pip install -r requirements.txt; then
        touch venv/.deps-installed
        echo ""
    else
        echo ""
        echo "ERROR: Dependency install failed (network problem?)."
        echo "Fix your connection and double-click start.command again."
        read -p "Press Enter to close..."
        exit 1
    fi
fi

# Kill any existing instance on port 5001
if lsof -ti:5001 > /dev/null 2>&1; then
    echo "Stopping existing RepoDoctor instance..."
    lsof -ti:5001 | xargs kill 2>/dev/null
    sleep 1
fi

echo "Starting RepoDoctor on http://127.0.0.1:5001"
echo "Press Ctrl+C to stop the server."
echo ""

# Open browser after a short delay
(sleep 2 && open "http://127.0.0.1:5001") &

# Start the Flask app
python app.py
