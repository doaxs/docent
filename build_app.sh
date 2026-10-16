#!/bin/bash
set -e
cd "$(dirname "$0")"
source venv/bin/activate
pip install -q -r requirements-desktop.txt
rm -rf build dist
pyinstaller Docent.spec
echo
echo "Built: dist/Docent.app"
echo "Run:   open dist/Docent.app"
