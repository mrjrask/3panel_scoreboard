#!/usr/bin/env bash
set -euo pipefail

python3 -m pip uninstall -y -r requirements.txt || true

echo "Uninstall complete."
