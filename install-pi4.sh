#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
RGB_MATRIX_REPO="${RGB_MATRIX_REPO:-https://github.com/hzeller/rpi-rgb-led-matrix.git}"

# rpi-rgb-led-matrix' Python package builds native extensions from source.
# Keep build tools explicit for Raspberry Pi OS Trixie Lite images.
echo "Updating apt package index..."
sudo apt update

echo "Installing system dependencies..."
sudo apt install -y \
  build-essential \
  cmake \
  cython3 \
  git \
  ninja-build \
  python-dev-is-python3 \
  python3 \
  python3-dev \
  python3-pil \
  python3-pip \
  python3-venv

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
python -m pip install --upgrade --force-reinstall --no-cache-dir "git+$RGB_MATRIX_REPO"

cat <<MSG

Install complete for Raspberry Pi 4 / rpi-rgb-led-matrix.

Quick launch (recommended):
  "$REPO_DIR/scoreboard"

The short launcher automatically uses the repo virtualenv, runs through sudo -E,
selects --backend rgbmatrix, and adds --led-no-hardware-pulse for Pi 4 audio/PWM
compatibility. You can pass any normal app option after the launcher command.

Common launcher presets:
  "$REPO_DIR/scoreboard"              # 3 horizontal panels (default)
  "$REPO_DIR/scoreboard" 3v           # 3 rotated vertical panels, clockwise
  "$REPO_DIR/scoreboard" 3v-ccw       # 3 rotated vertical panels, counter-clockwise
  "$REPO_DIR/scoreboard" 2            # 2 horizontal panels
  "$REPO_DIR/scoreboard" 2v           # 2 rotated vertical panels, clockwise
  "$REPO_DIR/scoreboard" 2v-ccw       # 2 rotated vertical panels, counter-clockwise

Useful options:
  "$REPO_DIR/scoreboard" --help                 # show all launcher presets/options
  "$REPO_DIR/scoreboard" --print-command        # preview the expanded sudo/python command
  "$REPO_DIR/scoreboard" --no-sudo              # run without sudo, if your setup allows it
  "$REPO_DIR/scoreboard" -- --help              # show application options from main.py
  "$REPO_DIR/scoreboard" 2v --port 80           # example: preset plus app option

Optional global command symlink for the quickest command:
  sudo ln -sf "$REPO_DIR/scoreboard" /usr/local/bin/scoreboard
  scoreboard
  scoreboard 2v --port 80

Install service:
  sudo cp "$REPO_DIR/systemd/scoreboard.service" /etc/systemd/system/scoreboard.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now scoreboard.service

Default Pi 4 runtime settings are tuned for three 64x32 P5 1/8-scan panels,
one panel per Triple Bonnet port using the Triple Bonnet/Active-3-compatible regular GPIO mapping:
  --rgb-layout parallel-ports --rgb-gpio-mapping regular --rgb-parallel 3 --rgb-chain-length 1 --rgb-multiplexing 1
MSG
