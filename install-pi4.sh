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

echo "Creating virtual environment at: $VENV_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip wheel setuptools
python -m pip install -r "$REPO_DIR/requirements.txt"
python -m pip install "git+$RGB_MATRIX_REPO"

cat <<MSG

Install complete for Raspberry Pi 4 / rpi-rgb-led-matrix.
Run as script:
  sudo -E env PATH="$VENV_DIR/bin:\$PATH" python "$REPO_DIR/main.py" --backend rgbmatrix

Install service:
  sudo cp "$REPO_DIR/systemd/scoreboard.service" /etc/systemd/system/scoreboard.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now scoreboard.service

Default Pi 4 runtime settings are tuned for three 64x32 P5 1/8-scan panels,
one panel per Triple Bonnet port using the Triple Bonnet/Active-3-compatible regular GPIO mapping:
  --rgb-layout parallel-ports --rgb-gpio-mapping regular --rgb-parallel 3 --rgb-chain-length 1 --rgb-multiplexing 1
MSG
