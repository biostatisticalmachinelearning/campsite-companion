#!/bin/bash
# Quick setup script for Campsite Companion
set -e

echo "=== Campsite Companion Setup ==="
echo ""

# Check Python version
python3 --version 2>/dev/null || { echo "Error: Python 3 is required. Install it from https://www.python.org/"; exit 1; }

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Installing dependencies..."
pip install -e . --quiet

# Check if catalog exists
if [ ! -f "data/catalog_recgov.json" ]; then
    echo ""
    read -p "Build the park catalog now? This is recommended to enable all features. It will take ~5 minutes. (y/n) " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Building catalog..."
        build-catalog
    else
        echo "Skipping catalog build. Run 'build-catalog' later to enable the Next Available Date Finder."
    fi
else
    echo "Park catalog already exists."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To start the web server:"
echo "  source .venv/bin/activate"
echo "  camping-web"
echo ""
echo "Then open http://localhost:8000 in your browser."
