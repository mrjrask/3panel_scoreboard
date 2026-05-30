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

echo "Ensuring bundled fonts are readable by the runtime service..."
# The scoreboard process may run under sudo/systemd or a dedicated service user.
# Keep the repository directories traversable and bundled BDF fonts readable so
# Pillow can load the crisp matrix fonts instead of falling back to its default.
find "$REPO_DIR" -type d -exec chmod a+rx {} +
find "$REPO_DIR/fonts" -type f -name '*.bdf' -exec chmod a+r {} +

python -m pip install --upgrade pip wheel setuptools
python -m pip install -r "$REPO_DIR/requirements.txt"
python -m pip install "git+$PIOMATTER_REPO"

cat <<MSG

Install complete for Raspberry Pi 5 / Blinka Piomatter.
Run as script:
  sudo -E env PATH="$VENV_DIR/bin:\$PATH" python "$REPO_DIR/main.py" --backend piomatter

Install service:
  sudo cp "$REPO_DIR/systemd/scoreboard.service" /etc/systemd/system/scoreboard.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now scoreboard.service
MSG
