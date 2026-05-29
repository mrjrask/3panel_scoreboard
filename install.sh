#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
PIOMATTER_REPO="${PIOMATTER_REPO:-https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter.git}"

echo "Updating apt package index..."
sudo apt update

echo "Installing system dependencies..."
sudo apt install -y python3 python3-pip python3-venv python3-dev python3-pil git

echo "Creating virtual environment at: $VENV_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip wheel setuptools
python -m pip install -r "$REPO_DIR/requirements.txt"
python -m pip install "git+$PIOMATTER_REPO"

cat <<MSG

Install complete.
Run as script:
  sudo -E env PATH="$VENV_DIR/bin:\$PATH" python "$REPO_DIR/main.py"

Install service:
  sudo cp "$REPO_DIR/systemd/scoreboard.service" /etc/systemd/system/scoreboard.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now scoreboard.service
MSG
