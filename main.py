#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import importlib
import importlib.metadata
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request
from PIL import BdfFontFile, Image, ImageDraw, ImageFont


DEFAULT_STATE_FILE = Path("scoreboard_state.json")
STATE_FILE = Path(os.environ.get("SCOREBOARD_STATE_FILE", DEFAULT_STATE_FILE))
FONT_FILE = Path(__file__).resolve().parent / "fonts" / "6x10.bdf"
FONT_PIXEL_SIZE = 10
SCORE_FONT_FILE = Path(__file__).resolve().parent / "fonts" / "10x20.bdf"
SCORE_FONT_PIXEL_SIZE = 20
SCORE_SCALE = 2
SEVEN_SEGMENT_SCORE_DIGIT_SIZE = (14, 24)
SEVEN_SEGMENT_SCORE_THICKNESS = 2
SEVEN_SEGMENT_INNING_DIGIT_SIZE = (7, 11)
SEVEN_SEGMENT_INNING_THICKNESS = 1
SEVEN_SEGMENT_DIGIT_GAP = 2
TEAM_NAME_FONT_FILE = Path(__file__).resolve().parent / "fonts" / "5x8.bdf"
TEAM_NAME_FONT_PIXEL_SIZE = 8

MAX_TEAM_CHARS = 10
MAX_INNINGS = 20
DEFAULT_TEXT_COLORS = {
    "team_a_name": "#FFFFFF",
    "team_a_score": "#FFB400",
    "team_b_name": "#FFFFFF",
    "team_b_score": "#FFFFFF",
    "inning_label": "#FFFFFF",
    "inning_value": "#FFFFFF",
    "count_labels": "#3CFF3C",
}
LOGGER = logging.getLogger("scoreboard")


def is_hex_color(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[1:])


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    if not is_hex_color(value):
        value = "#FFFFFF"
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def ensure_werkzeug_metadata_version() -> None:
    """Keep Flask startup working if Werkzeug files exist without dist-info metadata.

    Some Raspberry Pi installs can end up with an importable ``werkzeug`` package
    but no ``werkzeug-*.dist-info`` directory. Werkzeug's development server only
    uses that metadata to build its HTTP Server header, but it raises
    ``PackageNotFoundError`` during startup if the metadata is missing. Patch just
    that lookup so the scoreboard web controls still come up and log a repair hint.
    """
    try:
        importlib.metadata.version("werkzeug")
        return
    except importlib.metadata.PackageNotFoundError:
        pass

    werkzeug = importlib.import_module("werkzeug")
    fallback_version = getattr(werkzeug, "__version__", None) or "unknown"
    original_version = importlib.metadata.version

    def patched_version(distribution_name: str) -> str:
        normalized_name = distribution_name.lower().replace("_", "-")
        if normalized_name == "werkzeug":
            return fallback_version
        return original_version(distribution_name)

    importlib.metadata.version = patched_version
    LOGGER.warning(
        "Werkzeug is importable, but its package metadata is missing; using fallback "
        "version %r so the web UI can start. To repair the virtualenv, rerun the "
        "installer or run: python -m pip install --force-reinstall -r requirements.txt",
        fallback_version,
    )


@dataclass
class ScoreboardState:
    team_a: str = "AWAY TEAM"
    team_b: str = "HOME TEAM"
    score_a: int = 0
    score_b: int = 0
    inning: int = 1
    inning_half: str = "top"
    balls: int = 0
    strikes: int = 0
    outs: int = 0
    brightness: int = 70
    locked: bool = False
    text_colors: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_TEXT_COLORS)
    )
    batting_order_enabled: bool = True
    batting_order_a: int = 9
    batting_order_b: int = 9
    current_batter_a: int = 0
    current_batter_b: int = 0

    def clamp(self) -> None:
        self.team_a = (self.team_a or "AWAY TEAM").strip().upper()[:MAX_TEAM_CHARS]
        self.team_b = (self.team_b or "HOME TEAM").strip().upper()[:MAX_TEAM_CHARS]
        self.score_a = max(0, int(self.score_a))
        self.score_b = max(0, int(self.score_b))
        self.inning = min(max(1, int(self.inning)), MAX_INNINGS)
        self.balls = min(max(0, int(self.balls)), 3)
        self.strikes = min(max(0, int(self.strikes)), 2)
        self.outs = min(max(0, int(self.outs)), 2)
        self.brightness = min(max(5, int(self.brightness)), 100)
        self.locked = bool(self.locked)
        self.batting_order_enabled = bool(self.batting_order_enabled)
        self.batting_order_a = min(max(1, int(self.batting_order_a)), 20)
        self.batting_order_b = min(max(1, int(self.batting_order_b)), 20)
        self.current_batter_a = int(self.current_batter_a) % self.batting_order_a
        self.current_batter_b = int(self.current_batter_b) % self.batting_order_b
        sanitized_colors = dict(DEFAULT_TEXT_COLORS)
        if isinstance(self.text_colors, dict):
            for key, value in self.text_colors.items():
                if key in sanitized_colors and is_hex_color(value):
                    sanitized_colors[key] = value.upper()
        self.text_colors = sanitized_colors
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
            if self.strikes == 2:
                self._register_out()
            else:
                self.strikes += 1
        elif action == "outs_cycle":
            self._register_out()
        elif action == "reset":
            self.score_a = self.score_b = 0
            self.inning = 1
            self.inning_half = "top"
            self.balls = self.strikes = self.outs = 0
            self.current_batter_a = self.current_batter_b = 0
        elif action == "reset_scores":
            self.score_a = self.score_b = 0
        elif action == "reset_count":
            self.balls = self.strikes = 0
        elif action == "batter_a_advance":
            self.current_batter_a = (self.current_batter_a + 1) % self.batting_order_a
        elif action == "batter_b_advance":
            self.current_batter_b = (self.current_batter_b + 1) % self.batting_order_b
        elif action == "batter_current_advance":
            if self.inning_half == "top":
                self.current_batter_a = (
                    self.current_batter_a + 1
                ) % self.batting_order_a
            else:
                self.current_batter_b = (
                    self.current_batter_b + 1
                ) % self.batting_order_b
        elif action == "batters_reset_first":
            self.current_batter_a = self.current_batter_b = 0
        self.clamp()

    def _advance_half_inning(self) -> None:
        if self.inning_half == "top":
            self.inning_half = "bottom"
        else:
            self.inning_half = "top"
            self.inning += 1

    def _register_out(self) -> None:
        self.outs += 1
        self.balls = 0
        self.strikes = 0
        if self.outs >= 3:
            self.outs = 0
            self._advance_half_inning()

    def set_brightness(self, brightness: str | int) -> None:
        try:
            self.brightness = int(brightness)
        except (TypeError, ValueError):
            return
        self.clamp()

    def update_text_colors(self, values: dict[str, str]) -> None:
        for key in DEFAULT_TEXT_COLORS:
            value = values.get(key, "")
            if is_hex_color(value):
                self.text_colors[key] = value.upper()
        self.clamp()

    def set_batting_order(self, team_a_count: str, team_b_count: str) -> None:
        try:
            self.batting_order_a = int(team_a_count)
            self.batting_order_b = int(team_b_count)
        except (TypeError, ValueError):
            return
        self.clamp()


def set_state_file(path: str | Path) -> None:
    """Configure where scoreboard state is loaded from and saved to."""
    global STATE_FILE
    STATE_FILE = Path(path).expanduser()


def _fallback_state_files() -> list[Path]:
    """Return writable fallback locations for persistent state."""
    filename = STATE_FILE.name or DEFAULT_STATE_FILE.name
    candidates: list[Path] = []

    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        candidates.append(
            Path(xdg_state_home).expanduser() / "3panel_scoreboard" / filename
        )
    else:
        candidates.append(
            Path.home() / ".local" / "state" / "3panel_scoreboard" / filename
        )

    candidates.append(Path("/var/tmp/3panel_scoreboard") / filename)

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded != STATE_FILE and expanded not in seen:
            unique_candidates.append(expanded)
            seen.add(expanded)
    return unique_candidates


def _state_file_can_be_saved(state_file: Path) -> bool:
    """Best-effort preflight for deciding whether to prefer a fallback state file."""
    state_dir = state_file.parent if state_file.parent != Path("") else Path(".")
    if os.access(state_dir, os.W_OK):
        return True
    return state_file.exists() and os.access(state_file, os.W_OK)


def load_state() -> ScoreboardState:
    global STATE_FILE
    fallback_candidates = _fallback_state_files()
    if _state_file_can_be_saved(STATE_FILE):
        load_candidates = [STATE_FILE, *fallback_candidates]
    else:
        load_candidates = [*fallback_candidates, STATE_FILE]
    load_errors: list[str] = []

    for candidate in load_candidates:
        if not candidate.exists():
            continue
        try:
            s = ScoreboardState(**json.loads(candidate.read_text()))
            s.clamp()
            if candidate != STATE_FILE:
                LOGGER.warning(
                    "Loaded scoreboard state from fallback path %s because %s was not usable.",
                    candidate,
                    STATE_FILE,
                )
                STATE_FILE = candidate
            return s
        except Exception as exc:
            load_errors.append(f"{candidate}: {exc}")

    if load_errors:
        LOGGER.warning(
            "Failed to load saved scoreboard state from %s; starting with defaults.",
            "; ".join(load_errors),
        )
    return ScoreboardState()


def _restore_sudo_user_ownership(path: Path) -> None:
    """Keep state files editable by the user who launched the sudo process."""
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if not sudo_uid or not sudo_gid:
        return
    try:
        os.chown(path, int(sudo_uid), int(sudo_gid))
    except (OSError, ValueError) as exc:
        LOGGER.debug("Could not update ownership for %s: %s", path, exc)


def _state_parent(state_file: Path | None = None) -> Path:
    state_file = STATE_FILE if state_file is None else state_file
    return state_file.parent if state_file.parent != Path("") else Path(".")


def _write_state_file_directly(state_file: Path, state_payload: str) -> None:
    """Best-effort fallback for directories that cannot create temp files.

    Atomic replace needs write permission on the state directory. Some service
    deployments can still have an existing writable ``scoreboard_state.json``
    while the containing directory denies temporary-file creation. In that case,
    directly rewrite and fsync the existing state file so controls keep persisting
    instead of failing every save.
    """
    if not state_file.exists():
        raise FileNotFoundError(state_file)

    with state_file.open("w", encoding="utf-8") as state_file_handle:
        state_file_handle.write(state_payload)
        state_file_handle.write("\n")
        state_file_handle.flush()
        os.fsync(state_file_handle.fileno())
    _restore_sudo_user_ownership(state_file)


def _can_fallback_to_direct_write(exc: OSError, tmp_path: Path | None) -> bool:
    """Return whether a failed atomic save may safely try direct rewriting."""
    return tmp_path is None and exc.errno in {errno.EACCES, errno.EPERM}


def _can_fallback_to_alternate_state_file(exc: OSError) -> bool:
    """Return whether a save failure is likely limited to the configured path."""
    return exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}


def _save_state_payload_to_path(state_file: Path, state_payload: str) -> None:
    state_dir = _state_parent(state_file)
    tmp_path: Path | None = None

    try:
        if state_dir != Path("."):
            state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{state_file.stem}.",
            suffix=".tmp",
            dir=state_dir,
            text=True,
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(state_payload)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        _restore_sudo_user_ownership(tmp_path)
        tmp_path.replace(state_file)
        _restore_sudo_user_ownership(state_file)
        try:
            dir_fd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            LOGGER.debug("Could not fsync state directory %s: %s", state_dir, exc)
    except OSError as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if _can_fallback_to_direct_write(exc, tmp_path):
            try:
                _write_state_file_directly(state_file, state_payload)
                LOGGER.warning(
                    "Saved scoreboard state directly to %s after temp-file creation "
                    "failed: %s. For the safest persistence, make the state directory "
                    "writable by the scoreboard service.",
                    state_file,
                    exc,
                )
                return
            except OSError as fallback_exc:
                raise OSError(
                    exc.errno,
                    "atomic temp-file creation failed: "
                    f"{exc}; direct save failed: {fallback_exc}",
                ) from fallback_exc
        raise


def _save_state_payload_to_fallback(state_payload: str, original_exc: OSError) -> bool:
    global STATE_FILE
    original_state_file = STATE_FILE
    for fallback_state_file in _fallback_state_files():
        try:
            _save_state_payload_to_path(fallback_state_file, state_payload)
        except OSError as fallback_exc:
            LOGGER.debug(
                "Could not save scoreboard state to fallback path %s after %s failed: %s",
                fallback_state_file,
                original_state_file,
                fallback_exc,
            )
            continue

        STATE_FILE = fallback_state_file
        LOGGER.warning(
            "Unable to save scoreboard state to %s because of a permissions error: %s. "
            "Saved this and future changes to writable fallback path %s instead.",
            original_state_file,
            original_exc,
            fallback_state_file,
        )
        return True
    return False


def save_state(state: ScoreboardState) -> None:
    """Persist scoreboard state without reusing stale temporary files.

    Older runs could leave ``scoreboard_state.tmp`` behind with permissions that
    prevented the next web action from opening it. Create a unique temporary file
    in the same directory instead, then atomically replace the JSON state. If the
    directory blocks temporary-file creation but the existing state file itself is
    writable, fall back to an fsynced direct rewrite so persistence keeps working.
    If the configured path is not writable at all, move persistence to a per-user
    fallback path so controls continue to persist instead of failing every save.
    If persistence is still blocked by filesystem permissions, keep the in-memory
    scoreboard responsive and log the repair hint instead of returning HTTP 500.
    """
    state_payload = json.dumps(asdict(state))

    try:
        _save_state_payload_to_path(STATE_FILE, state_payload)
    except OSError as exc:
        can_use_fallback_path = _can_fallback_to_alternate_state_file(exc)
        if can_use_fallback_path and _save_state_payload_to_fallback(state_payload, exc):
            return
        LOGGER.error(
            "Unable to save scoreboard state to %s: atomic save failed: %s. "
            "Direct save was not attempted or did not succeed. Controls will keep "
            "working for this run, but changes will not persist. Check ownership of "
            "the state file and directory, or start with --state-file "
            "/path/to/writable/file. Remove any stale %s.*.tmp files if needed.",
            STATE_FILE,
            exc,
            STATE_FILE.stem,
        )


def load_bdf_font(font_file: Path) -> ImageFont.ImageFont:
    """Load an X11 BDF bitmap font without relying on FreeType BDF support."""
    with font_file.open("rb") as font_handle:
        return BdfFontFile.BdfFontFile(font_handle).to_imagefont()


def load_matrix_font(font_file: Path, pixel_size: int) -> ImageFont.ImageFont:
    """Load a crisp bitmap font for low-resolution RGB matrix text."""
    try:
        if font_file.suffix.lower() == ".bdf":
            return load_bdf_font(font_file)
        return ImageFont.truetype(font_file, pixel_size)
    except (OSError, SyntaxError, ValueError) as exc:
        LOGGER.warning(
            "Unable to load matrix font %s: %s; falling back to Pillow default font. "
            "Make sure the font file is readable and every parent directory is "
            "traversable by the scoreboard process; rerun the installer or run: "
            "chmod a+rx %s %s && chmod a+r %s",
            font_file,
            exc,
            font_file.parent.parent,
            font_file.parent,
            font_file,
        )
        return ImageFont.load_default()


def load_scoreboard_font() -> ImageFont.ImageFont:
    return load_matrix_font(FONT_FILE, FONT_PIXEL_SIZE)


def load_score_font() -> ImageFont.ImageFont:
    return load_matrix_font(SCORE_FONT_FILE, SCORE_FONT_PIXEL_SIZE)


def load_team_name_font() -> ImageFont.ImageFont:
    return load_matrix_font(TEAM_NAME_FONT_FILE, TEAM_NAME_FONT_PIXEL_SIZE)


def infer_addr_lines(
    panel_height: int, panel_scan: str, addr_lines_override: int | None
) -> int:
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


class MatrixDisplay:
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
        backend: str = "auto",
        rgb_gpio_mapping: str = "regular",
        rgb_slowdown_gpio: int = 2,
        rgb_multiplexing: int = 1,
        rgb_row_addr_type: int = 0,
        rgb_chain_length: int | None = None,
        rgb_parallel: int | None = None,
        rgb_pixel_mapper: str = "",
        rgb_layout: str = "parallel-ports",
        rgb_no_hardware_pulse: bool = False,
    ):
        self.width = width
        self.height = height
        self._framebuffer: bytearray | None = None
        self._rgbmatrix_panel_remap: tuple[int, int, int, int, int, int] | None = None
        self.backend_name: str = "unknown"
        self._driver = self._init_driver(
            width,
            height,
            bit_depth,
            chain_across,
            chain_down,
            addr_lines,
            serpentine,
            pinout_hint,
            backend,
            rgb_gpio_mapping,
            rgb_slowdown_gpio,
            rgb_multiplexing,
            rgb_row_addr_type,
            rgb_chain_length,
            rgb_parallel,
            rgb_pixel_mapper,
            rgb_layout,
            rgb_no_hardware_pulse,
        )

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
        backend: str,
        rgb_gpio_mapping: str,
        rgb_slowdown_gpio: int,
        rgb_multiplexing: int,
        rgb_row_addr_type: int,
        rgb_chain_length: int | None,
        rgb_parallel: int | None,
        rgb_pixel_mapper: str,
        rgb_layout: str,
        rgb_no_hardware_pulse: bool,
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
        if backend == "rgbmatrix":
            return self._init_rgbmatrix(
                width,
                height,
                bit_depth,
                chain_across,
                chain_down,
                rgb_gpio_mapping,
                rgb_slowdown_gpio,
                rgb_multiplexing,
                rgb_row_addr_type,
                rgb_chain_length,
                rgb_parallel,
                rgb_pixel_mapper,
                rgb_layout,
                rgb_no_hardware_pulse,
            )

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
                        mod = importlib.import_module(
                            f"adafruit_blinka_raspberry_pi5_piomatter.{module_name}"
                        )
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
                    self.backend_name = (
                        f"adafruit_blinka_raspberry_pi5_piomatter.{driver_cls.__name__}"
                    )
                    LOGGER.info(
                        "Initialized matrix backend: %s with args=%s",
                        self.backend_name,
                        kwargs,
                    )
                    return driver
                except TypeError as exc:
                    constructor_errors.append(f"{kwargs}: {exc}")

            raise RuntimeError(
                f"{driver_cls.__name__} constructor signature mismatch: "
                + " ; ".join(constructor_errors)
            )
        except Exception as exc:
            errors.append(f"adafruit...driver: {exc}")

        try:
            mod = importlib.import_module(
                "adafruit_blinka_raspberry_pi5_piomatter._piomatter"
            )
            pio_matter = getattr(mod, "PioMatter")
            colorspace_enum = getattr(mod, "Colorspace")
            pinout_enum = getattr(mod, "Pinout")
            geometry_cls = getattr(mod, "Geometry")

            panel_height = max(1, height // max(1, chain_down))
            n_addr_lines = (
                max(1, int(addr_lines))
                if addr_lines is not None
                else max(1, (panel_height // 2).bit_length() - 1)
            )

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
                raise RuntimeError(
                    "Geometry constructor signature mismatch: "
                    + " ; ".join(geometry_errors)
                )

            colorspace = _pick_enum(
                "RGB888", colorspace_enum, ("RGB565", "RGB666", "RGB")
            )
            # Prefer Triple Matrix Bonnet (Active3) pinouts when driving multiple panels
            # directly from the bonnet. Fall back for older/newer enum names.
            if pinout_hint == "active3":
                pinout = _pick_enum(
                    "Active3",
                    pinout_enum,
                    (
                        "ACTIVE3",
                        "Active3BGR",
                        "ACTIVE3BGR",
                        "ADAFRUIT_MATRIXBONNET",
                        "ADAFRUIT_FEATHERWING",
                        "DEFAULT",
                    ),
                )
            elif pinout_hint == "active3bgr":
                pinout = _pick_enum(
                    "Active3BGR",
                    pinout_enum,
                    (
                        "ACTIVE3BGR",
                        "Active3",
                        "ACTIVE3",
                        "ADAFRUIT_MATRIXBONNET",
                        "ADAFRUIT_FEATHERWING",
                        "DEFAULT",
                    ),
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
                    (
                        "ACTIVE3",
                        "Active3BGR",
                        "ACTIVE3BGR",
                        "ADAFRUIT_MATRIXBONNET",
                        "ADAFRUIT_FEATHERWING",
                        "DEFAULT",
                    ),
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
                    driver = pio_matter(
                        colorspace=colorspace,
                        pinout=pinout,
                        framebuffer=framebuffer,
                        geometry=geometry,
                    )
                    self._framebuffer = framebuffer
                    break
                except Exception as exc:
                    framebuffer_errors.append(
                        f"framebuffer bytes_per_pixel={bytes_per_pixel} (len={len(framebuffer)}): {exc}"
                    )

            if driver is None:
                raise RuntimeError(
                    "PioMatter framebuffer compatibility mismatch: "
                    + " ; ".join(framebuffer_errors)
                )

            if hasattr(driver, "bit_depth"):
                try:
                    driver.bit_depth = bit_depth
                except Exception:
                    pass
            self.backend_name = (
                "adafruit_blinka_raspberry_pi5_piomatter._piomatter.PioMatter"
            )
            LOGGER.info("Initialized matrix backend: %s", self.backend_name)
            return driver
        except Exception as exc:
            errors.append(f"adafruit..._piomatter: {exc}")

        if backend == "auto":
            try:
                return self._init_rgbmatrix(
                    width,
                    height,
                    bit_depth,
                    chain_across,
                    chain_down,
                    rgb_gpio_mapping,
                    rgb_slowdown_gpio,
                    rgb_multiplexing,
                    rgb_row_addr_type,
                    rgb_chain_length,
                    rgb_parallel,
                    rgb_pixel_mapper,
                    rgb_layout,
                    rgb_no_hardware_pulse,
                )
            except Exception as exc:
                errors.append(f"rgbmatrix.RGBMatrix: {exc}")

        raise RuntimeError(
            "Unable to initialize a supported HUB75 matrix driver. "
            "Install the Pi 5 Piomatter package or the Pi 4 rpi-rgb-led-matrix package, "
            "then choose --backend piomatter|rgbmatrix if auto-detection is ambiguous. "
            + " | ".join(errors)
        )

    def _init_rgbmatrix(
        self,
        width: int,
        height: int,
        bit_depth: int,
        chain_across: int,
        chain_down: int,
        gpio_mapping: str,
        slowdown_gpio: int,
        multiplexing: int,
        row_addr_type: int,
        chain_length_override: int | None,
        parallel_override: int | None,
        pixel_mapper: str,
        layout: str,
        no_hardware_pulse: bool,
    ):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        panel_cols = max(1, width // max(1, chain_across))
        panel_rows = max(1, height // max(1, chain_down))
        panel_count = max(1, chain_across * chain_down)

        if layout == "parallel-ports":
            parallel = max(
                1,
                int(
                    parallel_override if parallel_override is not None else panel_count
                ),
            )
            chain_length = max(
                1,
                int(chain_length_override if chain_length_override is not None else 1),
            )
            rows = panel_rows
            cols = panel_cols
        else:
            parallel = max(
                1,
                int(parallel_override if parallel_override is not None else chain_down),
            )
            chain_length = max(
                1,
                int(
                    chain_length_override
                    if chain_length_override is not None
                    else chain_across
                ),
            )
            rows = max(1, height // parallel)
            cols = max(1, width // chain_length)

        if gpio_mapping in {"adafruit-hat", "adafruit-hat-pwm"} and parallel > 1:
            raise ValueError(
                f"--rgb-gpio-mapping {gpio_mapping!r} only supports one rpi-rgb-led-matrix "
                f"parallel chain, but this geometry/layout requires {parallel}. "
                "For the Adafruit Triple LED Matrix Bonnet's three HUB75 outputs, "
                "use the Active-3-compatible default: --rgb-gpio-mapping regular. "
                "If using a single-output Adafruit HAT/Bonnet, either daisy-chain panels "
                "with --rgb-layout daisy-chain --rgb-parallel 1 --rgb-chain-length 3, or "
                "set --chain-across/--chain-down for a single output."
            )

        if layout == "parallel-ports" and chain_length == 1 and parallel == panel_count:
            canvas_width = cols * chain_length
            canvas_height = rows * parallel
            if (canvas_width, canvas_height) != (width, height):
                self._rgbmatrix_panel_remap = (
                    chain_across,
                    chain_down,
                    panel_cols,
                    panel_rows,
                    canvas_width,
                    canvas_height,
                )

        options = RGBMatrixOptions()
        options.hardware_mapping = gpio_mapping
        options.rows = rows
        options.cols = cols
        options.chain_length = chain_length
        options.parallel = parallel
        options.pwm_bits = bit_depth
        options.gpio_slowdown = max(0, int(slowdown_gpio))
        if multiplexing:
            options.multiplexing = int(multiplexing)
        if row_addr_type:
            options.row_address_type = int(row_addr_type)
        if pixel_mapper:
            options.pixel_mapper_config = pixel_mapper
        if no_hardware_pulse:
            options.disable_hardware_pulsing = True

        matrix = RGBMatrix(options=options)
        self.backend_name = "rgbmatrix.RGBMatrix"
        LOGGER.info(
            "Initialized matrix backend: %s rows=%s cols=%s chain_length=%s parallel=%s gpio_mapping=%s layout=%s",
            self.backend_name,
            rows,
            cols,
            chain_length,
            parallel,
            gpio_mapping,
            layout,
        )
        return matrix

    def show(self, image: Image.Image, brightness: int) -> None:
        if hasattr(self._driver, "brightness"):
            if self.backend_name.startswith("rgbmatrix"):
                self._driver.brightness = int(brightness)
            else:
                self._driver.brightness = brightness / 100.0
        if hasattr(self._driver, "show"):
            try:
                self._driver.show(image)
            except TypeError:
                self._blit_to_framebuffer(image)
                self._driver.show()
        elif hasattr(self._driver, "SetImage"):
            self._driver.SetImage(self._prepare_rgbmatrix_image(image))
        elif hasattr(self._driver, "image") and hasattr(self._driver, "refresh"):
            self._driver.image = image
            self._driver.refresh()
        else:
            raise RuntimeError("Matrix driver missing supported frame output method")

    def _prepare_rgbmatrix_image(self, image: Image.Image) -> Image.Image:
        rgb_image = image.convert("RGB")
        if self._rgbmatrix_panel_remap is None:
            return rgb_image

        (
            chain_across,
            chain_down,
            panel_cols,
            panel_rows,
            canvas_width,
            canvas_height,
        ) = self._rgbmatrix_panel_remap
        remapped = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))
        for panel_y in range(chain_down):
            for panel_x in range(chain_across):
                panel_index = panel_y * chain_across + panel_x
                src_box = (
                    panel_x * panel_cols,
                    panel_y * panel_rows,
                    (panel_x + 1) * panel_cols,
                    (panel_y + 1) * panel_rows,
                )
                remapped.paste(rgb_image.crop(src_box), (0, panel_index * panel_rows))
        return remapped

    def _blit_to_framebuffer(self, image: Image.Image) -> None:
        if self._framebuffer is None:
            raise RuntimeError(
                "Driver requires framebuffer updates but no framebuffer is available"
            )

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
        raise RuntimeError(
            f"Unsupported framebuffer size {len(self._framebuffer)} for {pixel_count} pixels"
        )


class MatrixRenderer:
    def __init__(self, display: MatrixDisplay, state: ScoreboardState):
        self.display = display
        self.state = state
        self.lock = threading.Lock()
        self.font = load_scoreboard_font()
        self.score_font = load_score_font()
        self.team_name_font = load_team_name_font()

    def draw(self) -> None:
        self.draw_mode("scoreboard")

    def draw_mode(self, mode: str = "scoreboard") -> None:
        with self.lock:
            image = Image.new(
                "RGB", (self.display.width, self.display.height), (0, 0, 0)
            )
            draw = ImageDraw.Draw(image)
            white, red, dim = (255, 255, 255), (255, 50, 50), (48, 48, 48)
            colors = {
                key: hex_to_rgb(value) for key, value in self.state.text_colors.items()
            }
            if mode == "panel_test":
                self._draw_panel_test(draw)
                self.display.show(image, self.state.brightness)
                return

            # Two layout modes:
            # - Vertical stack (64x96): 3 bands, one per panel.
            # - Horizontal row (192x32): 3 columns, one per panel.
            if self.display.height > self.display.width:
                panel_h = self.display.height // 3

                def block(
                    y: int,
                    team: str,
                    score: int,
                    name_key: str,
                    score_key: str,
                    batter: int,
                    lineup: int,
                ):
                    self._draw_team_name(draw, (2, y + 1), team, colors[name_key])
                    if self.state.batting_order_enabled:
                        self._draw_batting_order(
                            draw,
                            2,
                            self._batting_order_y(y, panel_h),
                            lineup,
                            batter,
                            colors[name_key],
                            dim,
                        )
                    score_text = str(score)
                    score_width, score_height = self._score_text_size(score_text)
                    score_y = y + max(0, (panel_h - score_height) // 2)
                    self._draw_score_text(
                        draw,
                        (self.display.width - score_width - 2, score_y),
                        score_text,
                        colors[score_key],
                    )

                block(
                    0,
                    self.state.team_a,
                    self.state.score_a,
                    "team_a_name",
                    "team_a_score",
                    self.state.current_batter_a,
                    self.state.batting_order_a,
                )
                block(
                    panel_h,
                    self.state.team_b,
                    self.state.score_b,
                    "team_b_name",
                    "team_b_score",
                    self.state.current_batter_b,
                    self.state.batting_order_b,
                )
                y = panel_h * 2
                half = "TOP" if self.state.inning_half == "top" else "BOT"
                half_color = colors["inning_value"]
                self._draw_inning_line(
                    draw,
                    2,
                    y + 2,
                    str(self.state.inning),
                    half,
                    colors["inning_label"],
                    colors["inning_value"],
                    half_color,
                )
                draw.text(
                    (2, y + 16),
                    f"B{self.state.balls} S{self.state.strikes}",
                    fill=colors["count_labels"],
                    font=self.font,
                )
                draw.text(
                    (2, y + 28), f"OUT {self.state.outs}", fill=red, font=self.font
                )
            else:
                panel_w = self.display.width // 3
                half = "TOP" if self.state.inning_half == "top" else "BOT"
                self._draw_team_panel(
                    draw,
                    0,
                    panel_w,
                    self.state.team_a,
                    self.state.score_a,
                    "team_a_name",
                    "team_a_score",
                    self.state.current_batter_a,
                    self.state.batting_order_a,
                    colors,
                    dim,
                )
                self._draw_team_panel(
                    draw,
                    panel_w,
                    panel_w,
                    self.state.team_b,
                    self.state.score_b,
                    "team_b_name",
                    "team_b_score",
                    self.state.current_batter_b,
                    self.state.batting_order_b,
                    colors,
                    dim,
                )
                half_color = colors["inning_value"]
                info_x = panel_w * 2 + 2
                self._draw_inning_line(
                    draw,
                    info_x,
                    1,
                    str(self.state.inning),
                    half,
                    colors["inning_label"],
                    colors["inning_value"],
                    half_color,
                )
                self._draw_count_dots(
                    draw,
                    info_x,
                    21,
                    "B",
                    self.state.balls,
                    3,
                    colors["count_labels"],
                    red,
                    dim,
                )
                self._draw_count_dots(
                    draw,
                    info_x + 23,
                    21,
                    "S",
                    self.state.strikes,
                    2,
                    colors["count_labels"],
                    red,
                    dim,
                )
                self._draw_count_dots(
                    draw,
                    info_x + 42,
                    21,
                    "O",
                    self.state.outs,
                    2,
                    colors["count_labels"],
                    red,
                    dim,
                )
            self.display.show(image, self.state.brightness)

    def _draw_inning_line(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        inning: str,
        half: str,
        label_color: tuple[int, int, int],
        value_color: tuple[int, int, int],
        half_color: tuple[int, int, int],
    ) -> None:
        inning_label = "INN"
        label_scale = 0.5
        label_gap = 1
        half_label = f"{half} "
        label_width, label_height = self._scaled_text_size(inning_label, label_scale)
        _, half_height = self._font_text_size(half_label, self.font)
        _, inning_height = self._inning_number_size(inning)
        line_height = max(half_height, inning_height)
        label_y = y + max(0, (line_height - label_height) // 2)
        half_y = y + max(0, (line_height - half_height) // 2)
        inning_y = y + max(0, (line_height - inning_height) // 2)

        self._draw_scaled_text(
            draw,
            (x, label_y),
            inning_label,
            label_color,
            label_scale,
        )
        half_x = x + label_width + label_gap
        draw.text((half_x, half_y), half_label, fill=half_color, font=self.font)
        inning_x = half_x + self._text_width(half_label)
        self._draw_inning_number(draw, (inning_x, inning_y), inning, value_color)

    def _text_width(self, text: str) -> int:
        width, _ = self._font_text_size(text, self.font)
        return width

    def _font_text_size(
        self, text: str, font: ImageFont.ImageFont
    ) -> tuple[int, int]:
        bbox = font.getbbox(text)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        return width, height

    def _team_name_size(self, text: str) -> tuple[int, int]:
        return self._font_text_size(text, self.team_name_font)

    def _draw_team_name(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: tuple[int, int, int],
    ) -> None:
        draw.text(xy, text, fill=fill, font=self.team_name_font)

    def _scaled_text_size(self, text: str, scale: float) -> tuple[int, int]:
        bbox = self.font.getbbox(text)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        return max(1, round(width * scale)), max(1, round(height * scale))

    def _draw_scaled_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: tuple[int, int, int],
        scale: float,
    ) -> None:
        bbox = self.font.getbbox(text)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.text((-bbox[0], -bbox[1]), text, fill=255, font=self.font)
        scaled_size = self._scaled_text_size(text, scale)
        if scaled_size != mask.size:
            mask = mask.resize(scaled_size, Image.Resampling.NEAREST)
        draw.bitmap(xy, mask, fill=fill)

    def _score_text_size(self, text: str) -> tuple[int, int]:
        return self._seven_segment_text_size(
            text,
            SEVEN_SEGMENT_SCORE_DIGIT_SIZE,
            SEVEN_SEGMENT_DIGIT_GAP,
        )

    def _draw_score_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: tuple[int, int, int],
    ) -> None:
        self._draw_seven_segment_text(
            draw,
            xy,
            text,
            fill,
            SEVEN_SEGMENT_SCORE_DIGIT_SIZE,
            SEVEN_SEGMENT_SCORE_THICKNESS,
            SEVEN_SEGMENT_DIGIT_GAP,
        )

    def _inning_number_size(self, text: str) -> tuple[int, int]:
        return self._seven_segment_text_size(
            text,
            SEVEN_SEGMENT_INNING_DIGIT_SIZE,
            1,
        )

    def _draw_inning_number(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: tuple[int, int, int],
    ) -> None:
        self._draw_seven_segment_text(
            draw,
            xy,
            text,
            fill,
            SEVEN_SEGMENT_INNING_DIGIT_SIZE,
            SEVEN_SEGMENT_INNING_THICKNESS,
            1,
        )

    def _seven_segment_text_size(
        self,
        text: str,
        digit_size: tuple[int, int],
        gap: int,
    ) -> tuple[int, int]:
        if not text:
            return 0, digit_size[1]
        digit_width, digit_height = digit_size
        width = len(text) * digit_width + max(0, len(text) - 1) * gap
        return width, digit_height

    def _draw_seven_segment_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: tuple[int, int, int],
        digit_size: tuple[int, int],
        thickness: int,
        gap: int,
    ) -> None:
        x, y = xy
        digit_width, _ = digit_size
        for char in text:
            self._draw_seven_segment_digit(
                draw, (x, y), char, fill, digit_size, thickness
            )
            x += digit_width + gap

    def _draw_seven_segment_digit(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        char: str,
        fill: tuple[int, int, int],
        digit_size: tuple[int, int],
        thickness: int,
    ) -> None:
        segments_by_digit = {
            "0": "abcfed",
            "1": "bc",
            "2": "abged",
            "3": "abgcd",
            "4": "fgbc",
            "5": "afgcd",
            "6": "afgecd",
            "7": "abc",
            "8": "abcdefg",
            "9": "abfgcd",
        }
        active_segments = segments_by_digit.get(char)
        if not active_segments:
            return

        x, y = xy
        width, height = digit_size
        t = max(1, thickness)
        mid_y = y + height // 2
        bottom_y = y + height - t
        right_x = x + width - t

        segment_rects = {
            "a": (x + t, y, x + width - t - 1, y + t - 1),
            "b": (right_x, y + t, x + width - 1, mid_y - 1),
            "c": (right_x, mid_y + 1, x + width - 1, bottom_y - 1),
            "d": (x + t, bottom_y, x + width - t - 1, y + height - 1),
            "e": (x, mid_y + 1, x + t - 1, bottom_y - 1),
            "f": (x, y + t, x + t - 1, mid_y - 1),
            "g": (x + t, mid_y, x + width - t - 1, mid_y + t - 1),
        }
        for segment in active_segments:
            draw.rectangle(segment_rects[segment], fill=fill)

    def _draw_team_panel(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        width: int,
        team: str,
        score: int,
        name_key: str,
        score_key: str,
        current_batter: int,
        lineup_size: int,
        colors: dict[str, tuple[int, int, int]],
        dim: tuple[int, int, int],
    ) -> None:
        team_y = 1
        self._draw_team_name(draw, (x + 2, team_y), team, colors[name_key])
        if self.state.batting_order_enabled:
            self._draw_batting_order(
                draw,
                x + 2,
                self._batting_order_y(0, self.display.height),
                lineup_size,
                current_batter,
                colors[name_key],
                dim,
            )
        score_text = str(score)
        score_width, score_height = self._score_text_size(score_text)
        score_x = x + width - score_width - 2
        score_y = max(0, (self.display.height - score_height) // 2)
        self._draw_score_text(
            draw,
            (max(x + 2, score_x), score_y),
            score_text,
            colors[score_key],
        )

    def _batting_order_y(self, panel_y: int, panel_height: int) -> int:
        return panel_y + panel_height - 1

    def _draw_batting_order(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        lineup_size: int,
        current_batter: int,
        active_color: tuple[int, int, int],
        inactive_color: tuple[int, int, int],
    ) -> None:
        for batter in range(lineup_size):
            fill = active_color if batter == current_batter else inactive_color
            draw.point((x + batter * 3, y), fill=fill)

    def _draw_count_dots(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        label: str,
        count: int,
        max_count: int,
        label_color: tuple[int, int, int],
        active_color: tuple[int, int, int],
        inactive_color: tuple[int, int, int],
    ) -> None:
        draw.text((x, y - 5), label, fill=label_color, font=self.font)
        for idx in range(max_count):
            dot_x = x + 8 + (idx * 5)
            fill = active_color if idx < count else inactive_color
            draw.ellipse((dot_x, y - 1, dot_x + 2, y + 1), fill=fill)

    def _draw_panel_test(self, draw: ImageDraw.ImageDraw) -> None:
        panel_w = max(1, self.display.width // 3)
        colors = ((255, 0, 0), (0, 220, 0), (0, 90, 255))
        labels = ("P1", "P2", "P3")
        for idx, (color, label) in enumerate(zip(colors, labels)):
            x0 = idx * panel_w
            x1 = min(self.display.width - 1, x0 + panel_w - 1)
            draw.rectangle((x0, 0, x1, self.display.height - 1), outline=color, width=1)
            draw.text((x0 + 2, 2), label, fill=color, font=self.font)
            draw.text(
                (x0 + 2, 14),
                color == colors[0] and "R" or color == colors[1] and "G" or "B",
                fill=color,
                font=self.font,
            )


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
details.card { padding:0; overflow:hidden; }
details.card > summary { cursor:pointer; list-style:none; padding:14px; font-size:1.15rem; font-weight:800; }
details.card > summary::-webkit-details-marker { display:none; }
details.card > summary::before { content:'▸'; display:inline-block; margin-right:8px; transition:transform 0.15s ease; }
details.card[open] > summary::before { transform:rotate(90deg); }
.section-body { padding:0 14px 14px; }
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
input[type=color] { height:44px; padding:4px; }
input[type=checkbox] { width:auto; }
input[type=range] { padding:0; }
.formgrid { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:10px; }
.inline { display:flex; align-items:center; gap:10px; margin:10px 0; }
label { display:block; margin:8px 0 6px; color:#c9d3e3; }
.small { font-size:0.9rem; color:#96a2b7; }
</style>
</head>
<body>
<div class='container'>
  <details class='card' data-section='score' open>
    <summary>Scoreboard Status</summary>
    <div class='section-body status'>
      <div>
        <div class='scoreline'>{{s.team_a}} {{s.score_a}} &nbsp;|&nbsp; {{s.team_b}} {{s.score_b}}</div>
        <div class='meta'>{{s.inning_half|upper}} {{s.inning}} • B{{s.balls}} S{{s.strikes}} O{{s.outs}}{% if s.batting_order_enabled %} • Batters A{{s.current_batter_a + 1}}/{{s.batting_order_a}} H{{s.current_batter_b + 1}}/{{s.batting_order_b}}{% endif %}</div>
      </div>
      <form method='post' action='/lock-toggle'>
        <button class='{{"unlock" if s.locked else "lock"}}'>{{"Unlock Controls" if s.locked else "Lock Controls"}}</button>
      </form>
    </div>
  </details>

  <details class='card' data-section='teams' open>
    <summary>Team Names</summary>
    <div class='section-body'>
      <form method='post' action='/rename'>
        <fieldset {{'disabled' if s.locked else ''}}>
          <label>Away Team</label><input name='team_a' value='{{s.team_a}}' maxlength='{{max_team_chars}}'>
          <label>Home Team</label><input name='team_b' value='{{s.team_b}}' maxlength='{{max_team_chars}}'>
          <div style='margin-top:10px;'><button>Save Team Names</button></div>
        </fieldset>
        {% if s.locked %}<p class='small'>Unlock controls to rename teams.</p>{% endif %}
      </form>
    </div>
  </details>

  <details class='card' data-section='layout-colors'>
    <summary>Layout & Colors</summary>
    <div class='section-body'>
      <form method='post' action='/config'>
        <fieldset {{'disabled' if s.locked else ''}}>
          <div class='formgrid'>
            {% for key,label in color_fields %}
              <div><label>{{label}}</label><input type='color' name='{{key}}' value='{{s.text_colors[key]}}'></div>
            {% endfor %}
          </div>
          <label for='brightness'>Brightness: <span id='brightness-value'>{{s.brightness}}</span>%</label>
          <input id='brightness' type='range' name='brightness' value='{{s.brightness}}' min='5' max='100' step='1'
                 data-brightness-url='{{url_for("brightness")}}'
                 oninput="document.getElementById('brightness-value').textContent = this.value">
          <div class='inline'><input type='checkbox' name='batting_order_enabled' value='1' {% if s.batting_order_enabled %}checked{% endif %}><label style='margin:0;'>Show batting-order tracker</label></div>
          <div class='formgrid'>
            <div><label>Away Lineup Size</label><input type='number' name='batting_order_a' value='{{s.batting_order_a}}' min='1' max='20'></div>
            <div><label>Home Lineup Size</label><input type='number' name='batting_order_b' value='{{s.batting_order_b}}' min='1' max='20'></div>
          </div>
          <div style='margin-top:10px;'><button>Save Layout & Colors</button></div>
        </fieldset>
      </form>
    </div>
  </details>

  <details class='card' data-section='controls' open>
    <summary>Controls</summary>
    <div class='section-body'>
      <fieldset {{'disabled' if s.locked else ''}}>
        <div class='grid'>
          {% for label,a,style in actions %}
            <form method='post' action='/action/{{a}}'><button class='{{style}}'>{{label}}</button></form>
          {% endfor %}
        </div>
        {% if s.locked %}<p class='small'>Controls are locked.</p>{% endif %}
      </fieldset>
    </div>
  </details>
</div>
<script>
(() => {
  const sectionStorageKey = 'scoreboard-section-open-state';

  function getStoredSectionState() {
    try {
      return JSON.parse(localStorage.getItem(sectionStorageKey) || '{}');
    } catch (error) {
      return {};
    }
  }

  function storeSectionState() {
    const state = {};
    document.querySelectorAll('details[data-section]').forEach((details) => {
      state[details.dataset.section] = details.open;
    });
    try {
      localStorage.setItem(sectionStorageKey, JSON.stringify(state));
    } catch (error) {
      // Keep controls working even if localStorage is blocked or unavailable.
    }
    return state;
  }

  function applySectionState(state) {
    document.querySelectorAll('details[data-section]').forEach((details) => {
      if (Object.prototype.hasOwnProperty.call(state, details.dataset.section)) {
        details.open = Boolean(state[details.dataset.section]);
      }
    });
  }

  function bindSectionToggles() {
    document.querySelectorAll('details[data-section]').forEach((details) => {
      details.addEventListener('toggle', storeSectionState);
    });
  }

  function bindBrightnessSlider() {
    const slider = document.getElementById('brightness');
    const valueLabel = document.getElementById('brightness-value');
    if (!slider || !valueLabel || slider.disabled) {
      return;
    }

    let saveTimer = null;
    let controller = null;

    function sendBrightness() {
      if (controller) {
        controller.abort();
      }
      controller = new AbortController();
      const formData = new FormData();
      formData.set('brightness', slider.value);
      fetch(slider.dataset.brightnessUrl, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch' },
        signal: controller.signal,
      })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Request failed with status ${response.status}`);
          }
          return response.json();
        })
        .then((data) => {
          if (Object.prototype.hasOwnProperty.call(data, 'brightness')) {
            slider.value = data.brightness;
            valueLabel.textContent = data.brightness;
          }
        })
        .catch((error) => {
          if (error.name !== 'AbortError') {
            console.error(error);
          }
        });
    }

    slider.addEventListener('input', () => {
      valueLabel.textContent = slider.value;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(sendBrightness, 120);
    });

    slider.addEventListener('change', () => {
      valueLabel.textContent = slider.value;
      clearTimeout(saveTimer);
      sendBrightness();
    });
  }

  function bindAjaxForms() {
    document.querySelectorAll('form[method="post"]').forEach((form) => {
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const scrollPosition = { x: window.scrollX, y: window.scrollY };
        const sectionState = storeSectionState();
        const submitter = event.submitter;
        if (submitter) {
          submitter.disabled = true;
        }

        try {
          const response = await fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'fetch' },
          });
          if (!response.ok) {
            throw new Error(`Request failed with status ${response.status}`);
          }

          const html = await response.text();
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const newContainer = doc.querySelector('.container');
          const currentContainer = document.querySelector('.container');
          if (!newContainer || !currentContainer) {
            throw new Error('Updated page content was not found.');
          }

          currentContainer.replaceWith(newContainer);
          applySectionState(sectionState);
          bindSectionToggles();
          bindBrightnessSlider();
          bindAjaxForms();
          window.scrollTo(scrollPosition.x, scrollPosition.y);
        } catch (error) {
          console.error(error);
          HTMLFormElement.prototype.submit.call(form);
        }
      });
    });
  }

  applySectionState(getStoredSectionState());
  bindSectionToggles();
  bindBrightnessSlider();
  bindAjaxForms();
})();
</script>
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
        ("Advance Current Batter", "batter_current_advance", ""),
        ("Away Batter +1", "batter_a_advance", ""),
        ("Home Batter +1", "batter_b_advance", ""),
        ("Reset Batters", "batters_reset_first", ""),
        ("Reset Count", "reset_count", ""),
        ("Reset Scores", "reset_scores", ""),
        ("Full Reset", "reset", "warn"),
    ]

    color_fields = [
        ("team_a_name", "Away Team Name"),
        ("team_a_score", "Away Team Score"),
        ("team_b_name", "Home Team Name"),
        ("team_b_score", "Home Team Score"),
        ("inning_label", "Inning Label"),
        ("inning_value", "Inning Value"),
        ("count_labels", "Count Labels"),
    ]

    @app.get("/")
    def index():
        return render_template_string(
            HTML,
            s=state,
            max_team_chars=MAX_TEAM_CHARS,
            actions=actions,
            color_fields=color_fields,
        )

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

    @app.post("/brightness")
    def brightness():
        with state_lock:
            if state.locked:
                return jsonify({"brightness": state.brightness, "locked": True}), 423
            state.set_brightness(request.form.get("brightness", state.brightness))
            save_state(state)
            renderer.draw()
            return jsonify({"brightness": state.brightness})

    @app.post("/config")
    def config():
        with state_lock:
            if state.locked:
                return redirect("/")
            state.update_text_colors(request.form)
            state.set_brightness(request.form.get("brightness", state.brightness))
            state.batting_order_enabled = (
                request.form.get("batting_order_enabled") == "1"
            )
            state.set_batting_order(
                request.form.get("batting_order_a", str(state.batting_order_a)),
                request.form.get("batting_order_b", str(state.batting_order_b)),
            )
            save_state(state)
            renderer.draw()
        return redirect("/")

    @app.get("/state")
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
    p.add_argument(
        "--backend",
        choices=("auto", "piomatter", "rgbmatrix"),
        default="auto",
        help="Matrix driver backend (Pi 5 uses piomatter; Pi 4 uses rgbmatrix)",
    )
    p.add_argument(
        "--addr-lines",
        type=int,
        default=None,
        help="Override HUB75 address lines (e.g. 4 for 1/8 scan 32px-tall panels)",
    )
    p.add_argument(
        "--panel-scan",
        choices=("auto", "1/8", "1/16", "1/32"),
        default="1/8",
        help="Panel scan ratio hint used to infer address lines when --addr-lines is omitted (repo default: 1/8 for common 64x32 P5 panels)",
    )
    p.add_argument(
        "--serpentine",
        action="store_true",
        help="Enable serpentine panel layout in low-level _piomatter fallback (usually OFF for Triple Bonnet direct-per-port wiring)",
    )
    p.add_argument(
        "--pinout",
        choices=("auto", "active3", "active3bgr", "matrixbonnet"),
        default="auto",
        help="Force low-level _piomatter pinout selection for diagnostics (default: auto)",
    )
    p.add_argument(
        "--rgb-gpio-mapping",
        default="regular",
        help="rpi-rgb-led-matrix GPIO mapping for Pi 4 installs (default: regular / Active-3 pinout for Triple Bonnet parallel outputs)",
    )
    p.add_argument(
        "--rgb-slowdown-gpio",
        type=int,
        default=2,
        help="rpi-rgb-led-matrix GPIO slowdown value",
    )
    p.add_argument(
        "--rgb-multiplexing",
        type=int,
        default=1,
        help="rpi-rgb-led-matrix multiplexing mode (default: 1 / Stripe for 64x32 P5 1/8-scan panels)",
    )
    p.add_argument(
        "--rgb-row-addr-type",
        type=int,
        default=0,
        help="rpi-rgb-led-matrix row address type override (0 keeps library default)",
    )
    p.add_argument(
        "--rgb-chain-length",
        type=int,
        default=None,
        help="Override rpi-rgb-led-matrix chain length",
    )
    p.add_argument(
        "--rgb-parallel",
        type=int,
        default=None,
        help="Override rpi-rgb-led-matrix parallel chain count",
    )
    p.add_argument(
        "--rgb-pixel-mapper",
        default="",
        help="rpi-rgb-led-matrix pixel mapper config, e.g. 'U-mapper;Rotate:90'",
    )
    p.add_argument(
        "--led-no-hardware-pulse",
        "--rgb-no-hardware-pulse",
        dest="rgb_no_hardware_pulse",
        action="store_true",
        help="Disable rpi-rgb-led-matrix hardware pulsing when snd_bcm2835 or other PWM users conflict (may increase flicker)",
    )
    p.add_argument(
        "--rgb-layout",
        choices=("parallel-ports", "daisy-chain"),
        default="parallel-ports",
        help="rpi-rgb-led-matrix topology (default: one 64x32 panel per Triple Bonnet port)",
    )
    p.add_argument(
        "--state-file",
        default=os.environ.get("SCOREBOARD_STATE_FILE", str(DEFAULT_STATE_FILE)),
        help="JSON file used for persistent scoreboard state (default: scoreboard_state.json, or SCOREBOARD_STATE_FILE)",
    )
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize LED driver, draw one frame, then exit (hardware diagnostics)",
    )
    p.add_argument(
        "--test-pattern",
        choices=("off", "panel"),
        default="off",
        help="Draw a startup diagnostic test pattern instead of scoreboard data (helps verify panel wiring/order/colors)",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = parse_args()
    set_state_file(args.state_file)
    state = load_state()
    state.brightness = args.brightness
    width = args.panel_width * args.chain_across
    height = args.panel_height * args.chain_down
    if args.chain_across == 1 and args.chain_down == 3:
        print("[scoreboard] Using vertical geometry (64x96).")
    elif args.chain_across == 3 and args.chain_down == 1:
        print("[scoreboard] Using horizontal geometry (192x32).")
    inferred_addr_lines = infer_addr_lines(
        args.panel_height, args.panel_scan, args.addr_lines
    )
    print(
        f"[scoreboard] geometry={width}x{height} panel={args.panel_width}x{args.panel_height} "
        f"backend={args.backend} scan={args.panel_scan} addr_lines={inferred_addr_lines} "
        f"serpentine={args.serpentine} pinout={args.pinout} "
        f"rgb_layout={args.rgb_layout} rgb_multiplexing={args.rgb_multiplexing} "
        f"rgb_no_hardware_pulse={args.rgb_no_hardware_pulse}"
    )
    print(
        "[scoreboard] Default panel-scan is 1/8 for this repo. Use --panel-scan auto|1/16|1/32 or --addr-lines to match other panel types."
    )
    display = MatrixDisplay(
        width,
        height,
        args.bit_depth,
        args.chain_across,
        args.chain_down,
        inferred_addr_lines,
        args.serpentine,
        args.pinout,
        args.backend,
        args.rgb_gpio_mapping,
        args.rgb_slowdown_gpio,
        args.rgb_multiplexing,
        args.rgb_row_addr_type,
        args.rgb_chain_length,
        args.rgb_parallel,
        args.rgb_pixel_mapper,
        args.rgb_layout,
        args.rgb_no_hardware_pulse,
    )
    renderer = MatrixRenderer(display, state)
    if args.test_pattern == "panel":
        renderer.draw_mode("panel_test")
        LOGGER.info("Rendered panel test pattern (P1/P2/P3, R/G/B)")
    else:
        renderer.draw()
    LOGGER.info("Initial frame rendered using backend=%s", display.backend_name)
    if args.init_only:
        LOGGER.info(
            "--init-only set; exiting after successful matrix initialization and first draw"
        )
        return
    ensure_werkzeug_metadata_version()
    create_app(state, renderer).run(host=args.listen, port=args.port)


if __name__ == "__main__":
    main()
