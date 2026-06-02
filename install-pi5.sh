#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
PIOMATTER_REPO="${PIOMATTER_REPO:-https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter.git}"

echo "Updating apt package index..."
sudo apt update

echo "Installing system dependencies..."
sudo apt install -y python3 python3-pip python3-venv python3-dev python3-pil git

echo "Creating or updating virtual environment at: $VENV_DIR"
if [[ -f "$VENV_DIR/pyvenv.cfg" ]]; then
  python3 -m venv --upgrade "$VENV_DIR"
else
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

echo "Ensuring bundled fonts are readable by the runtime service..."
# The scoreboard process may run under sudo/systemd or a dedicated service user.
# Keep the repository directories traversable and bundled BDF fonts readable so
# Pillow can load the crisp matrix fonts instead of falling back to its default.
find "$REPO_DIR" -type d -exec chmod a+rx {} +
find "$REPO_DIR/fonts" -type f -name '*.bdf' -exec chmod a+r {} +
chmod a+rx "$REPO_DIR/scoreboard"

echo "Updating Python packaging tools..."
python -m pip install --upgrade pip wheel setuptools

echo "Installing or updating Python dependencies from requirements.txt..."
python -m pip install --upgrade -r "$REPO_DIR/requirements.txt"

echo "Installing or updating hardware driver from latest git source..."
python -m pip install --upgrade --force-reinstall --no-cache-dir "git+$PIOMATTER_REPO"

cat <<MSG

Install complete for Raspberry Pi 5 / Blinka Piomatter.
Run manually with the short launcher:
  "$REPO_DIR/scoreboard" --backend piomatter

Optional global command symlink:
  sudo ln -sf "$REPO_DIR/scoreboard" /usr/local/bin/scoreboard
  scoreboard --backend piomatter

Install service:
  sudo cp "$REPO_DIR/systemd/scoreboard.service" /etc/systemd/system/scoreboard.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now scoreboard.service
MSG
