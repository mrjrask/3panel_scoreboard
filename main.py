#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request
from PIL import Image, ImageDraw, ImageFont

STATE_FILE = Path("scoreboard_state.json")
MAX_TEAM_CHARS = 5
LOGGER = logging.getLogger("scoreboard")


@dataclass
class ScoreboardState:
    team_a: str = "AWAY"
    team_b: str = "HOME"
    score_a: int = 0
    score_b: int = 0
    inning: int = 1
    inning_half: str = "top"
    balls: int = 0
    strikes: int = 0
    outs: int = 0
    brightness: int = 70
    locked: bool = False

    def clamp(self) -> None:
        self.team_a = (self.team_a or "AWAY").strip().upper()[:MAX_TEAM_CHARS]
        self.team_b = (self.team_b or "HOME").strip().upper()[:MAX_TEAM_CHARS]
        self.score_a = max(0, self.score_a)
        self.score_b = max(0, self.score_b)
        self.inning = max(1, self.inning)
        self.balls = min(max(0, self.balls), 3)
        self.strikes = min(max(0, self.strikes), 2)
        self.outs = min(max(0, self.outs), 2)
        self.brightness = min(max(5, int(self.brightness)), 100)
        self.locked = bool(self.locked)
        if self.inning_half not in {"top", "bottom"}:
            self.inning_half = "top"

    def update(self, action: str) -> None:
        if action == "score_a_inc":
            self.score_a += 1
        elif action == "score_a_dec":
            self.score_a -= 1
        elif action == "score_b_inc":
            self.score_b += 1
        elif action == "score_b_dec":
            self.score_b -= 1
        elif action == "inning_inc":
            self.inning += 1
        elif action == "inning_dec":
            self.inning -= 1
        elif action == "half_toggle":
            self.inning_half = "bottom" if self.inning_half == "top" else "top"
        elif action == "balls_cycle":
            self.balls = (self.balls + 1) % 4
        elif action == "strikes_cycle":
            self.strikes = (self.strikes + 1) % 3
        elif action == "outs_cycle":
            self.outs = (self.outs + 1) % 3
        elif action == "reset":
            self.score_a = self.score_b = 0
            self.inning = 1
            self.inning_half = "top"
            self.balls = self.strikes = self.outs = 0
        self.clamp()


def load_state() -> ScoreboardState:
    if STATE_FILE.exists():
        try:
            s = ScoreboardState(**json.loads(STATE_FILE.read_text()))
            s.clamp()
            return s
        except Exception as exc:
            LOGGER.warning("Failed to load saved state from %s: %s; starting with defaults.", STATE_FILE, exc)
    return ScoreboardState()


def save_state(state: ScoreboardState) -> None:
    tmp_path = STATE_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(asdict(state)))
    tmp_path.replace(STATE_FILE)


def infer_addr_lines(panel_height: int, panel_scan: str, addr_lines_override: int | None) -> int:
    """Infer HUB75 address lines for Piomatter geometry.

    Defaults in this repo target common 64x32 P5 1/8-scan baseball panels on
    a Triple Bonnet (4 address lines). For different panel scan ratios, pass
    --panel-scan explicitly or override with --addr-lines.
    """
    if addr_lines_override is not None:
        return max(1, int(addr_lines_override))

    if panel_scan == "1/8":
        return 4
    if panel_scan == "1/16":
        return 5
    if panel_scan == "1/32":
        return 6

    # Auto mode: 32px-tall P5 baseball panels are commonly 1/8-scan and need 4 address lines.
    if panel_height == 32:
        return 4
    return max(1, (max(1, panel_height) // 2).bit_length() - 1)

class PiomatterDisplay:
    def __init__(
        self,
        width: int,
        height: int,
        bit_depth: int,
        chain_across: int,
        chain_down: int,
        addr_lines: int | None = None,
        serpentine: bool = False,
        pinout_hint: str = "auto",
    ):
        self.width = width
        self.height = height
        self._framebuffer: bytearray | None = None
        self.backend_name: str = "unknown"
        self._driver = self._init_driver(width, height, bit_depth, chain_across, chain_down, addr_lines, serpentine, pinout_hint)

    def _init_driver(
        self,
        width: int,
        height: int,
        bit_depth: int,
        chain_across: int,
        chain_down: int,
        addr_lines: int | None,
        serpentine: bool,
        pinout_hint: str,
    ):
        def _pick_enum(default_name: str, enum_obj, fallbacks: tuple[str, ...]):
            names = (default_name, *fallbacks)
            for name in names:
                if hasattr(enum_obj, name):
                    return getattr(enum_obj, name)

            members_map = getattr(enum_obj, "__members__", None)
            if isinstance(members_map, dict) and members_map:
                for name in names:
                    if name in members_map:
                        return members_map[name]
                return next(iter(members_map.values()))

            for raw in (0, 1):
                try:
                    return enum_obj(raw)
                except Exception:
                    continue

            candidates = []
            for attr in dir(enum_obj):
                if attr.isupper():
                    try:
                        value = getattr(enum_obj, attr)
                    except Exception:
                        continue
                    if not callable(value):
                        candidates.append((attr, value))
            if candidates:
                for name in names:
                    for attr, value in candidates:
                        if attr == name:
                            return value
                return candidates[0][1]

            raise RuntimeError(f"Could not select a value from enum {enum_obj}")

        errors = []
        try:
            import piomatter
            self.backend_name = "piomatter.PioMatter"
            LOGGER.info("Initialized matrix backend: %s", self.backend_name)
            return piomatter.PioMatter(width=width, height=height, bit_depth=bit_depth)
        except Exception as exc:
            errors.append(f"piomatter.PioMatter: {exc}")

        try:
            pkg = importlib.import_module("adafruit_blinka_raspberry_pi5_piomatter")
            driver_cls = None

            for class_name in ("RGBMatrix", "PioMatter"):
                driver_cls = getattr(pkg, class_name, None)
                if driver_cls is not None:
                    break

            if driver_cls is None:
                for module_name in ("rgbmatrix", "piomatter"):
                    try:
                        mod = importlib.import_module(f"adafruit_blinka_raspberry_pi5_piomatter.{module_name}")
                    except Exception:
                        continue
                    for class_name in ("RGBMatrix", "PioMatter"):
                        driver_cls = getattr(mod, class_name, None)
                        if driver_cls is not None:
                            break
                    if driver_cls is not None:
                        break

            if driver_cls is None:
                exported = sorted(name for name in dir(pkg) if not name.startswith("_"))
                raise RuntimeError(
                    "No supported matrix driver class found (expected RGBMatrix or PioMatter); "
                    f"available exports: {', '.join(exported[:25])}"
                )

            init_arg_sets = (
                {
                    "width": width,
                    "height": height,
                    "bit_depth": bit_depth,
                    "chain_across": chain_across,
                    "chain_down": chain_down,
                },
                {
                    "width": width,
                    "height": height,
                    "bit_depth": bit_depth,
                    "chain_count": chain_across * chain_down,
                },
                {
                    "width": width,
                    "height": height,
                    "bit_depth": bit_depth,
                },
            )
            constructor_errors = []
            for kwargs in init_arg_sets:
                try:
                    driver = driver_cls(**kwargs)
                    self.backend_name = f"adafruit_blinka_raspberry_pi5_piomatter.{driver_cls.__name__}"
                    LOGGER.info("Initialized matrix backend: %s with args=%s", self.backend_name, kwargs)
                    return driver
                except TypeError as exc:
                    constructor_errors.append(f"{kwargs}: {exc}")

            raise RuntimeError(
                f"{driver_cls.__name__} constructor signature mismatch: " + " ; ".join(constructor_errors)
            )
        except Exception as exc:
            errors.append(f"adafruit...driver: {exc}")

        try:
            mod = importlib.import_module("adafruit_blinka_raspberry_pi5_piomatter._piomatter")
            pio_matter = getattr(mod, "PioMatter")
            colorspace_enum = getattr(mod, "Colorspace")
            pinout_enum = getattr(mod, "Pinout")
            geometry_cls = getattr(mod, "Geometry")

            panel_height = max(1, height // max(1, chain_down))
            n_addr_lines = max(1, int(addr_lines)) if addr_lines is not None else max(1, (panel_height // 2).bit_length() - 1)

            geometry = None
            geometry_errors = []
            geometry_arg_sets = (
                {
                    "width": width,
                    "height": height,
                    "n_addr_lines": n_addr_lines,
                },
                {
                    "width": width,
                    "height": height,
                    "n_addr_lines": n_addr_lines,
                    "serpentine": serpentine,
                },
                {
                    "width": width,
                    "height": height,
                    "n_addr_lines": n_addr_lines,
                    "n_temporal_planes": 2,
                },
            )
            for gkwargs in geometry_arg_sets:
                try:
                    geometry = geometry_cls(**gkwargs)
                    break
                except TypeError as exc:
                    geometry_errors.append(f"{gkwargs}: {exc}")

            if geometry is None:
                raise RuntimeError("Geometry constructor signature mismatch: " + " ; ".join(geometry_errors))

            colorspace = _pick_enum("RGB888", colorspace_enum, ("RGB565", "RGB666", "RGB"))
            # Prefer Triple Matrix Bonnet (Active3) pinouts when driving multiple panels
            # directly from the bonnet. Fall back for older/newer enum names.
            if pinout_hint == "active3":
                pinout = _pick_enum(
                    "Active3",
                    pinout_enum,
                    ("ACTIVE3", "Active3BGR", "ACTIVE3BGR", "ADAFRUIT_MATRIXBONNET", "ADAFRUIT_FEATHERWING", "DEFAULT"),
                )
            elif pinout_hint == "active3bgr":
                pinout = _pick_enum(
                    "Active3BGR",
                    pinout_enum,
                    ("ACTIVE3BGR", "Active3", "ACTIVE3", "ADAFRUIT_MATRIXBONNET", "ADAFRUIT_FEATHERWING", "DEFAULT"),
                )
            elif pinout_hint == "matrixbonnet":
                pinout = _pick_enum(
                    "ADAFRUIT_MATRIXBONNET",
                    pinout_enum,
                    ("DEFAULT", "Active3", "ACTIVE3", "ADAFRUIT_FEATHERWING"),
                )
            elif chain_across * chain_down >= 2:
                pinout = _pick_enum(
                    "Active3",
                    pinout_enum,
                    ("ACTIVE3", "Active3BGR", "ACTIVE3BGR", "ADAFRUIT_MATRIXBONNET", "ADAFRUIT_FEATHERWING", "DEFAULT"),
                )
            else:
                pinout = _pick_enum(
                    "ADAFRUIT_MATRIXBONNET",
                    pinout_enum,
                    ("Active3", "ACTIVE3", "ADAFRUIT_FEATHERWING", "DEFAULT"),
                )
            LOGGER.info("Selected Piomatter pinout enum value: %s", pinout)
            driver = None
            framebuffer_errors = []
            for bytes_per_pixel in (4, 3):
                framebuffer = bytearray(width * height * bytes_per_pixel)
                try:
                    driver = pio_matter(colorspace=colorspace, pinout=pinout, framebuffer=framebuffer, geometry=geometry)
                    self._framebuffer = framebuffer
                    break
                except Exception as exc:
                    framebuffer_errors.append(
                        f"framebuffer bytes_per_pixel={bytes_per_pixel} (len={len(framebuffer)}): {exc}"
                    )

            if driver is None:
                raise RuntimeError("PioMatter framebuffer compatibility mismatch: " + " ; ".join(framebuffer_errors))

            if hasattr(driver, "bit_depth"):
                try:
                    driver.bit_depth = bit_depth
                except Exception:
                    pass
            self.backend_name = "adafruit_blinka_raspberry_pi5_piomatter._piomatter.PioMatter"
            LOGGER.info("Initialized matrix backend: %s", self.backend_name)
            return driver
        except Exception as exc:
            errors.append(f"adafruit..._piomatter: {exc}")

        raise RuntimeError(
            "Unable to initialize Blinka Pi5 Piomatter driver. "
            "Install Adafruit_Blinka_Raspberry_Pi5_Piomatter and verify API compatibility. "
            + " | ".join(errors)
        )

    def show(self, image: Image.Image, brightness: int) -> None:
        if hasattr(self._driver, "brightness"):
            self._driver.brightness = brightness / 100.0
        if hasattr(self._driver, "show"):
            try:
                self._driver.show(image)
            except TypeError:
                self._blit_to_framebuffer(image)
                self._driver.show()
        elif hasattr(self._driver, "image") and hasattr(self._driver, "refresh"):
            self._driver.image = image
            self._driver.refresh()
        else:
            raise RuntimeError("Piomatter driver missing supported frame output method")

    def _blit_to_framebuffer(self, image: Image.Image) -> None:
        if self._framebuffer is None:
            raise RuntimeError("Driver requires framebuffer updates but no framebuffer is available")

        rgb_bytes = image.convert("RGB").tobytes("raw", "RGB")
        pixel_count = self.width * self.height
        if len(self._framebuffer) == pixel_count * 3:
            self._framebuffer[:] = rgb_bytes
            return
        if len(self._framebuffer) == pixel_count * 4:
            out = bytearray(pixel_count * 4)
            for idx in range(pixel_count):
                src = idx * 3
                dst = idx * 4
                out[dst : dst + 3] = rgb_bytes[src : src + 3]
            self._framebuffer[:] = out
            return
        raise RuntimeError(f"Unsupported framebuffer size {len(self._framebuffer)} for {pixel_count} pixels")


class MatrixRenderer:
    def __init__(self, display: PiomatterDisplay, state: ScoreboardState):
        self.display = display
        self.state = state
        self.lock = threading.Lock()
        self.font = ImageFont.load_default()

    def draw(self) -> None:
        self.draw_mode("scoreboard")

    def draw_mode(self, mode: str = "scoreboard") -> None:
        with self.lock:
            image = Image.new("RGB", (self.display.width, self.display.height), (0, 0, 0))
            draw = ImageDraw.Draw(image)
            white, amber, red, green = (255, 255, 255), (255, 180, 0), (255, 50, 50), (60, 255, 60)
            if mode == "panel_test":
                self._draw_panel_test(draw)
                self.display.show(image, self.state.brightness)
                return

            # Two layout modes:
            # - Vertical stack (64x96): 3 bands, one per panel.
            # - Horizontal row (192x32): 3 columns, one per panel.
            if self.display.height > self.display.width:
                panel_h = self.display.height // 3

                def block(y: int, title: str, team: str, score: int):
                    draw.text((2, y + 2), title, fill=white, font=self.font)
                    draw.text((2, y + 16), team, fill=white, font=self.font)
                    draw.text((self.display.width - 12, y + 16), str(score), fill=amber, font=self.font)

                block(0, "AWAY", self.state.team_a, self.state.score_a)
                block(panel_h, "HOME", self.state.team_b, self.state.score_b)
                y = panel_h * 2
                half = "TOP" if self.state.inning_half == "top" else "BOT"
                draw.text((2, y + 2), f"{half} {self.state.inning}", fill=white, font=self.font)
                draw.text((2, y + 16), f"B{self.state.balls} S{self.state.strikes}", fill=green, font=self.font)
                draw.text((2, y + 28), f"OUT {self.state.outs}", fill=red, font=self.font)
            else:
                panel_w = self.display.width // 3
                half = "TOP" if self.state.inning_half == "top" else "BOT"
                draw.text((2, 2), f"A {self.state.team_a} {self.state.score_a}", fill=amber, font=self.font)
                draw.text((panel_w + 2, 2), f"H {self.state.team_b} {self.state.score_b}", fill=white, font=self.font)
                draw.text((panel_w * 2 + 2, 2), f"{half} {self.state.inning}", fill=white, font=self.font)
                draw.text((panel_w * 2 + 2, 16), f"B{self.state.balls} S{self.state.strikes} O{self.state.outs}", fill=green, font=self.font)
            self.display.show(image, self.state.brightness)

    def _draw_panel_test(self, draw: ImageDraw.ImageDraw) -> None:
        panel_w = max(1, self.display.width // 3)
        colors = ((255, 0, 0), (0, 220, 0), (0, 90, 255))
        labels = ("P1", "P2", "P3")
        for idx, (color, label) in enumerate(zip(colors, labels)):
            x0 = idx * panel_w
            x1 = min(self.display.width - 1, x0 + panel_w - 1)
            draw.rectangle((x0, 0, x1, self.display.height - 1), outline=color, width=1)
            draw.text((x0 + 2, 2), label, fill=color, font=self.font)
            draw.text((x0 + 2, 14), color == colors[0] and "R" or color == colors[1] and "G" or "B", fill=color, font=self.font)


HTML = """<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Baseball Scoreboard</title>
<style>
:root { color-scheme: dark; }
body { font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:0; background:#0b0d12; color:#eef2f7; }
.container { max-width: 760px; margin:0 auto; padding: 16px; }
.card { background:#151a23; border:1px solid #283040; border-radius:14px; padding:14px; margin-bottom:12px; }
.status { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
.scoreline { font-size:1.2rem; font-weight:700; }
.meta { color:#a9b5c9; }
.grid { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:10px; }
button { width:100%; border:none; border-radius:12px; padding:14px 12px; font-size:1.05rem; font-weight:700; background:#27324a; color:#f3f7ff; }
button:active { transform:scale(0.99); }
button.warn { background:#7a2d2d; }
button.lock { background:#355d2f; }
button.unlock { background:#8d6a24; }
fieldset { border:none; padding:0; margin:0; }
input { width:100%; border-radius:10px; border:1px solid #3a4357; background:#0f141d; color:#fff; padding:10px; font-size:1rem; box-sizing:border-box; }
label { display:block; margin:8px 0 6px; color:#c9d3e3; }
.small { font-size:0.9rem; color:#96a2b7; }
</style>
</head>
<body>
<div class='container'>
  <div class='card status'>
    <div>
      <div class='scoreline'>{{s.team_a}} {{s.score_a}} &nbsp;|&nbsp; {{s.team_b}} {{s.score_b}}</div>
      <div class='meta'>{{s.inning_half|upper}} {{s.inning}} • B{{s.balls}} S{{s.strikes}} O{{s.outs}}</div>
    </div>
    <form method='post' action='/lock-toggle'>
      <button class='{{"unlock" if s.locked else "lock"}}'>{{"Unlock Controls" if s.locked else "Lock Controls"}}</button>
    </form>
  </div>

  <div class='card'>
    <form method='post' action='/rename'>
      <fieldset {{'disabled' if s.locked else ''}}>
        <label>Away Team</label><input name='team_a' value='{{s.team_a}}' maxlength='{{max_team_chars}}'>
        <label>Home Team</label><input name='team_b' value='{{s.team_b}}' maxlength='{{max_team_chars}}'>
        <div style='margin-top:10px;'><button>Save Team Names</button></div>
      </fieldset>
      {% if s.locked %}<p class='small'>Unlock controls to rename teams.</p>{% endif %}
    </form>
  </div>

  <div class='card'>
    <fieldset {{'disabled' if s.locked else ''}}>
      <div class='grid'>
        {% for label,a,style in actions %}
          <form method='post' action='/action/{{a}}'><button class='{{style}}'>{{label}}</button></form>
        {% endfor %}
      </div>
      {% if s.locked %}<p class='small'>Controls are locked.</p>{% endif %}
    </fieldset>
  </div>
</div>
</body>
</html>"""


def create_app(state: ScoreboardState, renderer: MatrixRenderer) -> Flask:
    app = Flask(__name__)
    state_lock = threading.Lock()

    actions = [
        ("Away +1", "score_a_inc", ""),
        ("Away -1", "score_a_dec", ""),
        ("Home +1", "score_b_inc", ""),
        ("Home -1", "score_b_dec", ""),
        ("Inning +1", "inning_inc", ""),
        ("Inning -1", "inning_dec", ""),
        ("Toggle Top/Bot", "half_toggle", ""),
        ("Cycle Balls", "balls_cycle", ""),
        ("Cycle Strikes", "strikes_cycle", ""),
        ("Cycle Outs", "outs_cycle", ""),
        ("Reset", "reset", "warn"),
    ]

    @app.get("/")
    def index():
        return render_template_string(HTML, s=state, max_team_chars=MAX_TEAM_CHARS, actions=actions)

    @app.post("/lock-toggle")
    def lock_toggle():
        with state_lock:
            state.locked = not state.locked
            state.clamp()
            save_state(state)
        return redirect("/")

    @app.post("/action/<action>")
    def action(action: str):
        with state_lock:
            if state.locked:
                return redirect("/")
            state.update(action)
            save_state(state)
            renderer.draw()
        return redirect("/")

    @app.post("/rename")
    def rename():
        with state_lock:
            if state.locked:
                return redirect("/")
            state.team_a = request.form.get("team_a", state.team_a)
            state.team_b = request.form.get("team_b", state.team_b)
            state.clamp()
            save_state(state)
            renderer.draw()
        return redirect("/")

    @app.get('/state')
    def get_state():
        return jsonify(asdict(state))

    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel-width", type=int, default=64)
    p.add_argument("--panel-height", type=int, default=32)
    # Triple Bonnet default: 3 panels side-by-side (192x32 total).
    p.add_argument("--chain-across", type=int, default=3)
    p.add_argument("--chain-down", type=int, default=1)
    p.add_argument("--bit-depth", type=int, default=6)
    p.add_argument("--brightness", type=int, default=70)
    p.add_argument("--addr-lines", type=int, default=None, help="Override HUB75 address lines (e.g. 4 for 1/8 scan 32px-tall panels)")
    p.add_argument("--panel-scan", choices=("auto", "1/8", "1/16", "1/32"), default="1/8", help="Panel scan ratio hint used to infer address lines when --addr-lines is omitted (repo default: 1/8 for common 64x32 P5 panels)")
    p.add_argument("--serpentine", action="store_true", help="Enable serpentine panel layout in low-level _piomatter fallback (usually OFF for Triple Bonnet direct-per-port wiring)")
    p.add_argument(
        "--pinout",
        choices=("auto", "active3", "active3bgr", "matrixbonnet"),
        default="auto",
        help="Force low-level _piomatter pinout selection for diagnostics (default: auto)",
    )
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--init-only", action="store_true", help="Initialize LED driver, draw one frame, then exit (hardware diagnostics)")
    p.add_argument(
        "--test-pattern",
        choices=("off", "panel"),
        default="off",
        help="Draw a startup diagnostic test pattern instead of scoreboard data (helps verify panel wiring/order/colors)",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()
    state = load_state()
    state.brightness = args.brightness
    width = args.panel_width * args.chain_across
    height = args.panel_height * args.chain_down
    if args.chain_across == 1 and args.chain_down == 3:
        print("[scoreboard] Using vertical geometry (64x96).")
    elif args.chain_across == 3 and args.chain_down == 1:
        print("[scoreboard] Using horizontal geometry (192x32).")
    inferred_addr_lines = infer_addr_lines(args.panel_height, args.panel_scan, args.addr_lines)
    print(
        f"[scoreboard] geometry={width}x{height} panel={args.panel_width}x{args.panel_height} "
        f"scan={args.panel_scan} addr_lines={inferred_addr_lines} serpentine={args.serpentine} pinout={args.pinout}"
    )
    print("[scoreboard] Default panel-scan is 1/8 for this repo. Use --panel-scan auto|1/16|1/32 or --addr-lines to match other panel types.")
    display = PiomatterDisplay(
        width,
        height,
        args.bit_depth,
        args.chain_across,
        args.chain_down,
        inferred_addr_lines,
        args.serpentine,
        args.pinout,
    )
    renderer = MatrixRenderer(display, state)
    if args.test_pattern == "panel":
        renderer.draw_mode("panel_test")
        LOGGER.info("Rendered panel test pattern (P1/P2/P3, R/G/B)")
    else:
        renderer.draw()
    LOGGER.info("Initial frame rendered using backend=%s", display.backend_name)
    if args.init_only:
        LOGGER.info("--init-only set; exiting after successful matrix initialization and first draw")
        return
    create_app(state, renderer).run(host=args.listen, port=args.port)


if __name__ == "__main__":
    main()
