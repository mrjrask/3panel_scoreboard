# 3-Panel Baseball Scoreboard

This project targets a 3-panel HUB75 baseball scoreboard using:

- Adafruit Triple LED Matrix Bonnet
- 3x 32x64 P5 1/8-scan HUB75 panels
- Raspberry Pi OS Trixie 64-bit Lite

Two Raspberry Pi installers are provided:

- **Raspberry Pi 5:** `install-pi5.sh` installs the Adafruit Blinka Raspberry Pi5 Piomatter driver.
- **Raspberry Pi 4:** `install-pi4.sh` installs the `rpi-rgb-led-matrix` Python bindings from <https://github.com/hzeller/rpi-rgb-led-matrix.git>.

## Install on Raspberry Pi 5 (Blinka Piomatter)

```bash
./install-pi5.sh
```

Run as a script:

```bash
sudo -E env PATH="$PWD/.venv/bin:$PATH" python main.py --backend piomatter
```

## Install on Raspberry Pi 4 (rpi-rgb-led-matrix)

```bash
./install-pi4.sh
```

Run as a script:

```bash
sudo -E env PATH="$PWD/.venv/bin:$PATH" python main.py --backend rgbmatrix
```

If startup exits because the Pi sound module (`snd_bcm2835`) is loaded, either disable built-in Pi audio or add the `rpi-rgb-led-matrix` compatibility flag exposed by this app:

```bash
sudo -E env PATH="$PWD/.venv/bin:$PATH" python main.py --backend rgbmatrix --led-no-hardware-pulse
```

`--led-no-hardware-pulse` is also available as `--rgb-no-hardware-pulse`. It avoids the sound/PWM conflict, but may increase display flicker compared with disabling built-in audio.

By default, the Pi 4 backend is tuned for this project's three-panel P5 1/8-scan setup: one 64x32 panel per Triple Bonnet port using the Triple Bonnet/Active-3-compatible `regular` GPIO mapping. The app renders a logical 192x32 scoreboard, then remaps each 64x32 third of that image onto the three `rpi-rgb-led-matrix` parallel outputs. The effective default `rgbmatrix` topology is:

```bash
python main.py --backend rgbmatrix \
  --rgb-layout parallel-ports \
  --rgb-gpio-mapping regular \
  --rgb-slowdown-gpio 2 \
  --rgb-multiplexing 1 \
  --rgb-row-addr-type 0
```

That creates the equivalent of `--rgb-parallel 3 --rgb-chain-length 1` automatically for the default `--chain-across 3 --chain-down 1` geometry. To add one extra panel to the output connector of each directly connected Triple Bonnet panel and mirror the same content onto the chained panel, keep the logical scoreboard geometry at 192x32 and set the mirror chain length to 2:

```bash
python main.py --backend rgbmatrix --rgb-mirror-chain-length 2
```

With the default geometry, that drives the equivalent of `--rgb-parallel 3 --rgb-chain-length 2` on the hardware while duplicating each 64x32 logical panel image into both physical panels on that output chain. Use a larger mirror chain length only if every output has more mirrored panels in the chain.

Do not use `--rgb-gpio-mapping adafruit-hat` for this three-output mode; the single-output Adafruit HAT/Bonnet mapping only supports `--rgb-parallel 1`. If you instead daisy-chain three unique scoreboard panels from one HUB75 output, use:

```bash
python main.py --backend rgbmatrix --rgb-layout daisy-chain --rgb-parallel 1 --rgb-chain-length 3
```

If your physical panel arrangement still needs coordinate remapping, pass the library's pixel mapper string, for example:

```bash
python main.py --backend rgbmatrix --rgb-pixel-mapper 'U-mapper;Rotate:90'
```

## Two-panel option

Use `--two-panel` when only the panels plugged into Triple Bonnet ports 1 and 2 should be used:

```bash
python main.py --two-panel
```

The flag defaults the horizontal geometry to two 64x32 panels (`--chain-across 2 --chain-down 1`) unless you explicitly provide another geometry. The away and home team panels keep their usual team name, batting-order tracker, and score placement on ports 1 and 2. The inning, balls, strikes, and outs indicators move to the row three pixels above the batting-order tracker:

- **Top of inning:** balls/strikes/outs appear on panel 1, and the inning number appears on panel 2.
- **Bottom of inning:** the inning number appears on panel 1, and balls/strikes/outs appear on panel 2.

For Pi 4 / `rpi-rgb-led-matrix`, this also defaults to the equivalent of `--rgb-parallel 2 --rgb-chain-length 1` so the app drives the first two Triple Bonnet ports and does not allocate a third info panel.

The two-panel layout can also be used with rotated vertical screens:

```bash
python main.py --two-panel --vertical-screen
```

That renders two logical 32x64 panels and rotates them onto the physical 64x32 hardware. The team name, score, and batting-order tracker stay on the same two Triple Bonnet ports; balls/strikes/outs are stacked near the top of one panel while the inning number is shown near the top of the other panel. Top-of-inning puts balls/strikes/outs on panel 1 and the inning on panel 2; bottom-of-inning swaps them. Use `--two-panel --screen-orientation vertical-ccw` if your two rotated panels are mounted in the opposite direction.

## Scoreboard features

This Raspberry Pi/Triple Bonnet adaptation carries forward the manual baseball scoreboard controls from `mrjrask/scoreboard_i75w`:

- Away and home team names up to 10 characters (defaults: `AWAY TEAM` and `HOME TEAM`).
- Per-section color controls for away/home names and scores, inning value and count labels.
- Team score increment/decrement controls.
- Inning increment/decrement controls and top/bottom toggle.
- Balls (0-3), strikes (0-2), and outs (0-2), including baseball-style strikeout/out advancement and half-inning rollover after three outs.
- Optional batting-order tracker per team with configurable lineup sizes from 1-20 batters and controls to advance/reset batters.
- Full reset, plus score-only and count-only reset controls.
- Automatic JSON state persistence after each change. State writes are flushed and fsynced before the atomic replace so recent web-control changes survive board resets/reboots and abrupt power loss as reliably as the underlying filesystem allows. If the service can update an existing state file but cannot create temporary files in the state directory, it falls back to an fsynced direct rewrite and logs a warning instead of losing every change.

## Web UI

After startup, open:

```text
http://<pi-ip>:8080/
```

## Run as systemd service

1. Copy the repo to `/opt/scoreboard_3panel_pi5` (or update `systemd/scoreboard.service` paths).
2. Install dependencies with the Raspberry Pi-specific installer:
   - Pi 5: `./install-pi5.sh`
   - Pi 4: `./install-pi4.sh`
3. Install the unit:

```bash
sudo cp systemd/scoreboard.service /etc/systemd/system/scoreboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now scoreboard.service
```

For Pi 4 service installs, either rely on backend auto-detection or add `--backend rgbmatrix` plus any needed `--rgb-*` options to the `ExecStart` line.

### State persistence path and permissions

By default, the app reads and writes `scoreboard_state.json` in its working directory. You can choose a different writable location with either:

```bash
python main.py --state-file /path/to/scoreboard_state.json
SCOREBOARD_STATE_FILE=/path/to/scoreboard_state.json python main.py
```

The included systemd unit passes an explicit `/opt/scoreboard_3panel_pi5/scoreboard_state.json` path so persistence does not depend on the process working directory. If logs mention `Permission denied: '.'`, make sure the state directory is writable by the service user, or pre-create `scoreboard_state.json` with write permission for that user so the direct-write fallback can still persist changes.

### Font file permission warnings

If startup logs warnings such as `Unable to load matrix font ... Permission denied`, the app is running but Pillow cannot read the bundled BDF fonts, so the scoreboard falls back to Pillow's default font. Re-run the Pi installer to repair repository permissions, or run this from the repo directory on the Pi:

```bash
find "$PWD" -type d -exec chmod a+rx {} +
find "$PWD/fonts" -type f -name '*.bdf' -exec chmod a+r {} +
```

Directories need the execute/traverse bit and the `.bdf` files need the read bit for whichever user starts `main.py` or the systemd service.

## Geometry and panel scan notes

- Default display shape is 192x32 (3x panels across).
- If the same three physical 64x32 panels are mounted as vertical screens, use `--vertical-screen` to render each panel as a logical 32x64 screen and rotate each panel image onto the hardware before display:

```bash
python main.py --vertical-screen
```

  `--vertical-screen` is a shortcut for `--screen-orientation vertical-cw`. If your mounted panels are rotated the opposite direction, use:

```bash
python main.py --screen-orientation vertical-ccw
```

- Override geometry if needed:

```bash
python main.py --panel-width 64 --panel-height 32 --chain-across 3 --chain-down 1
```

- This repo defaults to `--panel-scan 1/8` and, for Pi 4 / `rpi-rgb-led-matrix`, `--rgb-multiplexing 1`. That is the first-pass default for the requested 64x32 P5 1/8-scan panels, even though these panels often require panel-specific tuning.

```bash
python main.py
```

- If your panels are not 1/8-scan, set the scan hint explicitly so address lines are inferred correctly:

```bash
python main.py --panel-scan auto   # heuristic (good fallback if scan ratio is unknown)
python main.py --panel-scan 1/16   # many 64x32 indoor panels
python main.py --panel-scan 1/32   # some higher multiplex panels
```

- If needed, force address lines directly (highest priority over scan hint):

```bash
python main.py --addr-lines 4
```

- For Adafruit Triple LED Matrix Bonnet with one panel directly on each of the 3 bonnet ports, keep `--serpentine` OFF (default) for the Piomatter backend.

- For Pi 4 / `rpi-rgb-led-matrix`, `--rgb-mirror-chain-length 2` supports one mirrored daisy-chained panel on each Triple Bonnet output without changing the 192x32 web UI or scoreboard layout.

- If panel wiring snakes between connectors (daisy-chained/snake layout), try enabling serpentine layout for the Piomatter backend:

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
3. If colors are wrong on Pi 5 / Piomatter, try alternate pinout:
   ```bash
   python main.py --backend piomatter --panel-scan 1/8 --pinout active3bgr --test-pattern panel
   ```
4. If rows/sections are wrong, force address lines explicitly:
   ```bash
   python main.py --panel-scan 1/8 --addr-lines 4 --test-pattern panel
   ```
5. If using Pi 4 / rpi-rgb-led-matrix, the default is already one panel per bonnet port. Verify that default first:
   ```bash
   python main.py --backend rgbmatrix --panel-scan 1/8 --test-pattern panel
   ```
6. If the Pi 4 output is scrambled, keep the one-panel-per-port topology and sweep the common P5 1/8-scan tuning values:
   ```bash
   python main.py --backend rgbmatrix --panel-scan 1/8 --rgb-multiplexing 1 --test-pattern panel
   python main.py --backend rgbmatrix --panel-scan 1/8 --rgb-multiplexing 4 --test-pattern panel
   python main.py --backend rgbmatrix --panel-scan 1/8 --rgb-row-addr-type 3 --test-pattern panel
   ```
7. If the panels appear in the wrong arrangement after the default per-port remap, test the library pixel mapper option:
   ```bash
   python main.py --backend rgbmatrix --panel-scan 1/8 --rgb-pixel-mapper 'U-mapper;Rotate:90' --test-pattern panel
   ```
