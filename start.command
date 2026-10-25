#!/bin/bash
# RepoDoctor — Mac Launcher
# Double-click this file to start RepoDoctor and open the browser.

# Navigate to the project directory
cd "$(dirname "$0")" || { echo "ERROR: Could not find RepoDoctor directory."; read -p "Press Enter to close..."; exit 1; }

echo "========================================"
echo "  RepoDoctor — Starting up..."
echo "========================================"
echo ""

# Make sure git is installed (ships with the Xcode command line tools).
if ! command -v git > /dev/null 2>&1; then
    echo "ERROR: Git is not installed."
    echo "Install it by running this in Terminal:  xcode-select --install"
    echo "then double-click start.command again."
    read -p "Press Enter to close..."
    exit 1
fi

# Pull the latest code from main so a double-click always runs the newest
# version — same promise the Windows launcher (start.ps1) already keeps.
echo "Pulling latest code from main..."
git pull origin main
if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: git pull failed (network issue, or local edits blocking the merge)."
    echo "See RUN_LOCAL.md for how to fix, then double-click start.command again."
    read -p "Press Enter to close..."
    exit 1
fi
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv || { echo "ERROR: Python 3 not found. Install from python.org or: brew install python@3.12"; read -p "Press Enter to close..."; exit 1; }
fi
source venv/bin/activate

# Install dependencies until they succeed (stamp file), and again whenever
# requirements.txt is newer than the stamp — the launcher pulls main on every
# run, so a newly added package must get installed or the app won't start.
if [ ! -f "venv/.deps-installed" ] || [ requirements.txt -nt venv/.deps-installed ]; then
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

# Kill any existing instance on port 5001. LISTEN only: plain `lsof -ti:5001`
# also matches the browser's open connections to the app and would kill it.
if lsof -ti tcp:5001 -sTCP:LISTEN > /dev/null 2>&1; then
    echo "Stopping existing RepoDoctor instance..."
    lsof -ti tcp:5001 -sTCP:LISTEN | xargs kill 2>/dev/null
    sleep 1
fi

echo "Starting RepoDoctor on http://127.0.0.1:5001"
echo "Press Ctrl+C to stop the server."
echo ""

# Open browser after a short delay
(sleep 2 && open "http://127.0.0.1:5001") &

# Start the Flask app
python app.py
