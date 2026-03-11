#!/bin/bash
set -e

echo "=== RepoDoctor Netlify Build ==="

# 1. Create publish directory with static files
echo "Copying static files to dist/..."
mkdir -p dist/static
cp -r static/* dist/static/

# 2. Create the function bundle directory
# Netlify Python functions: directory name = function name
echo "Creating function bundle..."
FUNC_DIR="netlify_functions/api"
mkdir -p "$FUNC_DIR"

# Copy all Python modules
cp app.py "$FUNC_DIR/"
cp github_client.py "$FUNC_DIR/"
cp models.py "$FUNC_DIR/"
cp security.py "$FUNC_DIR/"
cp ai_analyzer.py "$FUNC_DIR/"
cp spec_cleaner.py "$FUNC_DIR/"
cp project_mapper.py "$FUNC_DIR/"

# Copy the serverless handler
cp netlify_handler.py "$FUNC_DIR/api.py"

# Copy templates (Flask needs these at runtime)
cp -r templates "$FUNC_DIR/"

# Create data and config dirs (ephemeral in serverless)
mkdir -p "$FUNC_DIR/data/specs"
mkdir -p "$FUNC_DIR/config"

# Copy config if it exists
if [ -d "config" ] && [ "$(ls -A config 2>/dev/null)" ]; then
    cp -r config/* "$FUNC_DIR/config/"
fi

# Create requirements.txt for the function
cp requirements.txt "$FUNC_DIR/requirements.txt"

echo "=== Build complete ==="
echo "  - Static files in dist/"
echo "  - Function bundle in $FUNC_DIR/"
