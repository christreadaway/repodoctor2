#!/bin/bash
# RepDoctor2 — Mac Launcher
# Double-click this file to start RepDoctor2 and open the browser.

# Navigate to the project directory
cd "$(dirname "$0")" || { echo "ERROR: Could not find RepDoctor2 directory."; read -p "Press Enter to close..."; exit 1; }

echo "========================================"
echo "  RepDoctor2 — Starting up..."
echo "========================================"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv || { echo "ERROR: Python 3 not found. Install from python.org or: brew install python@3.12"; read -p "Press Enter to close..."; exit 1; }
    echo "Installing dependencies..."
    source venv/bin/activate
    pip install -r requirements.txt
    echo ""
else
    source venv/bin/activate
fi

# Kill any existing instance on port 5001
if lsof -ti:5001 > /dev/null 2>&1; then
    echo "Stopping existing RepDoctor2 instance..."
    lsof -ti:5001 | xargs kill 2>/dev/null
    sleep 1
fi

echo "Starting RepDoctor2 on http://127.0.0.1:5001"
echo "Press Ctrl+C to stop the server."
echo ""

# Open browser after a short delay
(sleep 2 && open "http://127.0.0.1:5001") &

# Start the Flask app
python app.py
