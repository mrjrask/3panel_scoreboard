# Raspberry Pi 5 Baseball Scoreboard (Blinka Piomatter + Triple Bonnet)

This project is rewritten for **Raspberry Pi 5** on **Raspberry Pi OS Trixie 64-bit Lite**, using:

- Adafruit Triple LED Matrix Bonnet
- 3x 32x64 P5 1/8-scan HUB75 panels
- Adafruit Blinka Raspberry Pi5 Piomatter driver

## Install

```bash
./install.sh
```

## Run as script

```bash
sudo -E env PATH="$PWD/.venv/bin:$PATH" python main.py
```

Web UI: `http://<pi-ip>:8080/`

## Run as systemd service

1. Copy repo to `/opt/scoreboard_3panel_pi5` (or update service paths).
2. Install dependencies via `./install.sh`.
3. Install unit:

```bash
sudo cp systemd/scoreboard.service /etc/systemd/system/scoreboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now scoreboard.service
```

## Notes

- Default display shape is 192x32 (3x panels across).
- Override geometry if needed:

```bash
python main.py --panel-width 64 --panel-height 32 --chain-across 3 --chain-down 1
```

- This repo now defaults to `--panel-scan 1/8`, which is safest for common 64x32 P5 baseball panels on Triple Bonnet setups.

```bash
python main.py
```

- If your panels are not 1/8-scan, set scan hint explicitly so address lines are inferred correctly:

```bash
python main.py --panel-scan auto   # heuristic (good fallback if scan ratio is unknown)
python main.py --panel-scan 1/16   # many 64x32 indoor panels
python main.py --panel-scan 1/32   # some higher multiplex panels
```

- If needed, force address lines directly (highest priority over scan hint):

```bash
python main.py --addr-lines 4
```

- For Adafruit Triple LED Matrix Bonnet with one panel directly on each of the 3 bonnet ports, keep `--serpentine` OFF (default).

- If panel wiring snakes between connectors (daisy-chained/snake layout), try enabling serpentine layout:

```bash
python main.py --serpentine
```

## 1/8-scan panel troubleshooting workflow

If output is scrambled, mirrored, wrong color order, or panels appear swapped:

1. Verify baseline startup:
   ```bash
   python main.py --panel-scan 1/8 --init-only
   ```
2. Draw the built-in panel test pattern to verify physical panel order and color channels:
   ```bash
   python main.py --panel-scan 1/8 --test-pattern panel
   ```
   Expected on a 3-panel horizontal setup (left→right): **P1 red**, **P2 green**, **P3 blue**.
3. If colors are wrong, try alternate pinout:
   ```bash
   python main.py --panel-scan 1/8 --pinout active3bgr --test-pattern panel
   ```
4. If rows/sections are wrong, force address lines explicitly:
   ```bash
   python main.py --panel-scan 1/8 --addr-lines 4 --test-pattern panel
   ```
5. If your wiring is daisy-chained/snake-like instead of one panel per bonnet port, test:
   ```bash
   python main.py --panel-scan 1/8 --serpentine --test-pattern panel
   ```
