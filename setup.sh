#!/bin/bash
# Quick setup script for Camping Reservation Search
set -e

echo "=== Camping Reservation Search Setup ==="
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

# Set up .env if it doesn't exist
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo "Created .env from .env.example."
        echo ">>> IMPORTANT: Edit .env and add your Recreation.gov RIDB API key."
        echo ">>> Get a free key at: https://ridb.recreation.gov/"
        echo ""
    fi
else
    echo ".env file already exists."
fi

# Check if catalog exists
if [ ! -f "data/catalog_recgov.json" ]; then
    echo ""
    read -p "Build the park catalog now? This takes ~5 minutes. (y/n) " -n 1 -r
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
