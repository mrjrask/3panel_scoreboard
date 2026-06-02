import errno
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import main


class SaveStateTests(unittest.TestCase):
    def setUp(self):
        self.original_state_file = main.STATE_FILE
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.addCleanup(main.set_state_file, self.original_state_file)

    def test_save_state_writes_json_atomically(self):
        state_file = Path(self.tmpdir.name) / "scoreboard_state.json"
        main.set_state_file(state_file)

        state = main.ScoreboardState(score_a=3, score_b=2, team_a="vis", team_b="home")
        state.clamp()
        main.save_state(state)

        saved = json.loads(state_file.read_text())
        self.assertEqual(saved["score_a"], 3)
        self.assertEqual(saved["score_b"], 2)
        self.assertEqual(saved["team_a"], "VIS")
        self.assertEqual(saved["team_b"], "HOME")
        self.assertEqual(list(state_file.parent.glob("*.tmp")), [])

    def test_save_state_falls_back_to_existing_file_when_temp_create_is_denied(self):
        state_file = Path(self.tmpdir.name) / "scoreboard_state.json"
        state_file.write_text('{"score_a": 0}\n')
        main.set_state_file(state_file)

        state = main.ScoreboardState(score_a=7, score_b=4)
        with mock.patch.object(
            main.tempfile,
            "mkstemp",
            side_effect=PermissionError(13, "Permission denied", "."),
        ):
            main.save_state(state)

        saved = json.loads(state_file.read_text())
        self.assertEqual(saved["score_a"], 7)
        self.assertEqual(saved["score_b"], 4)

    def test_save_state_does_not_directly_rewrite_after_temp_write_failure(self):
        state_file = Path(self.tmpdir.name) / "scoreboard_state.json"
        original_payload = '{"score_a": 5, "score_b": 6}\n'
        state_file.write_text(original_payload)
        main.set_state_file(state_file)

        state = main.ScoreboardState(score_a=9, score_b=8)
        with (
            mock.patch.object(
                main.os,
                "fsync",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ),
            self.assertLogs(main.LOGGER, level="ERROR") as logs,
        ):
            main.save_state(state)

        self.assertEqual(state_file.read_text(), original_payload)
        self.assertIn("Direct save was not attempted", "\n".join(logs.output))
        self.assertEqual(list(state_file.parent.glob("*.tmp")), [])

    def test_save_state_does_not_directly_rewrite_after_temp_create_enospc(self):
        state_file = Path(self.tmpdir.name) / "scoreboard_state.json"
        original_payload = '{"score_a": 1, "score_b": 2}\n'
        state_file.write_text(original_payload)
        main.set_state_file(state_file)

        state = main.ScoreboardState(score_a=4, score_b=3)
        with (
            mock.patch.object(
                main.tempfile,
                "mkstemp",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ),
            self.assertLogs(main.LOGGER, level="ERROR") as logs,
        ):
            main.save_state(state)

        self.assertEqual(state_file.read_text(), original_payload)
        self.assertIn("Direct save was not attempted", "\n".join(logs.output))

    def test_save_state_uses_writable_fallback_when_configured_path_is_denied(self):
        denied_parent = Path(self.tmpdir.name) / "denied"
        denied_parent.mkdir()
        state_file = denied_parent / "scoreboard_state.json"
        fallback_home = Path(self.tmpdir.name) / "state-home"
        main.set_state_file(state_file)
        original_mkstemp = main.tempfile.mkstemp

        def fail_primary_only(*args, **kwargs):
            if Path(kwargs["dir"]) == denied_parent:
                raise PermissionError(13, "Permission denied", str(denied_parent))
            return original_mkstemp(*args, **kwargs)

        state = main.ScoreboardState(score_a=11, score_b=10)
        with (
            mock.patch.dict(main.os.environ, {"XDG_STATE_HOME": str(fallback_home)}),
            mock.patch.object(main.tempfile, "mkstemp", side_effect=fail_primary_only),
            self.assertLogs(main.LOGGER, level="WARNING") as logs,
        ):
            main.save_state(state)

        fallback_file = fallback_home / "3panel_scoreboard" / "scoreboard_state.json"
        saved = json.loads(fallback_file.read_text())
        self.assertEqual(saved["score_a"], 11)
        self.assertEqual(saved["score_b"], 10)
        self.assertEqual(main.STATE_FILE, fallback_file)
        self.assertIn("Saved this and future changes", "\n".join(logs.output))


class MatrixFontLoadingTests(unittest.TestCase):
    def test_load_matrix_font_uses_bdf_parser_for_bdf_fonts(self):
        with mock.patch.object(
            main.ImageFont,
            "truetype",
            side_effect=OSError("cannot open resource"),
        ) as truetype:
            font = main.load_matrix_font(main.FONT_FILE, main.FONT_PIXEL_SIZE)

        truetype.assert_not_called()
        self.assertEqual(font.getbbox("ABC"), (0, 0, 18, 10))

    def test_load_matrix_font_falls_back_only_when_bdf_parser_fails(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".bdf") as font_file,
            self.assertLogs(main.LOGGER, level="WARNING") as logs,
        ):
            Path(font_file.name).write_text("not a bdf font")

            font = main.load_matrix_font(Path(font_file.name), 10)

        self.assertEqual(len(font.getbbox("ABC")), 4)
        self.assertIn("Unable to load matrix font", "\n".join(logs.output))


class MatrixDisplayMirrorChainTests(unittest.TestCase):
    def test_rgbmatrix_parallel_ports_mirrors_each_panel_to_chained_output(self):
        captured_options = []

        class FakeRGBMatrixOptions:
            pass

        class FakeRGBMatrix:
            def __init__(self, options):
                captured_options.append(options)

        fake_rgbmatrix = types.SimpleNamespace(
            RGBMatrix=FakeRGBMatrix, RGBMatrixOptions=FakeRGBMatrixOptions
        )

        with mock.patch.dict(sys.modules, {"rgbmatrix": fake_rgbmatrix}):
            display = main.MatrixDisplay(
                192,
                32,
                6,
                3,
                1,
                backend="rgbmatrix",
                rgb_mirror_chain_length=2,
            )

        self.assertEqual(captured_options[0].chain_length, 2)
        self.assertEqual(captured_options[0].parallel, 3)
        self.assertEqual(captured_options[0].rows, 32)
        self.assertEqual(captured_options[0].cols, 64)

        source = main.Image.new("RGB", (192, 32), (0, 0, 0))
        draw = main.ImageDraw.Draw(source)
        panel_colors = ((255, 0, 0), (0, 220, 0), (0, 90, 255))
        for idx, color in enumerate(panel_colors):
            draw.rectangle((idx * 64, 0, idx * 64 + 63, 31), fill=color)

        remapped = display._prepare_rgbmatrix_image(source)
        self.assertEqual(remapped.size, (128, 96))
        pixels = remapped.load()
        for row, color in enumerate(panel_colors):
            y = row * 32 + 10
            self.assertEqual(pixels[10, y], color)
            self.assertEqual(pixels[74, y], color)


class RotatedPanelDisplayTests(unittest.TestCase):
    class FakePhysicalDisplay:
        width = 192
        height = 32
        backend_name = "fake"

        def __init__(self):
            self.images = []

        def show(self, image, brightness):
            self.images.append((image.copy(), brightness))

    def test_vertical_screen_rotates_each_logical_panel_clockwise(self):
        physical_display = self.FakePhysicalDisplay()
        display = main.RotatedPanelDisplay(
            physical_display, 64, 32, 3, 1, "vertical-cw"
        )
        self.assertEqual((display.width, display.height), (96, 64))

        source = main.Image.new("RGB", (96, 64), (0, 0, 0))
        pixels = source.load()
        pixels[0, 0] = (255, 0, 0)
        pixels[32, 0] = (0, 220, 0)
        pixels[64, 0] = (0, 90, 255)

        display.show(source, 42)

        physical, brightness = physical_display.images[-1]
        self.assertEqual(brightness, 42)
        self.assertEqual(physical.size, (192, 32))
        physical_pixels = physical.load()
        self.assertEqual(physical_pixels[63, 0], (255, 0, 0))
        self.assertEqual(physical_pixels[127, 0], (0, 220, 0))
        self.assertEqual(physical_pixels[191, 0], (0, 90, 255))

    def test_vertical_screen_rotates_each_logical_panel_counterclockwise(self):
        physical_display = self.FakePhysicalDisplay()
        display = main.RotatedPanelDisplay(
            physical_display, 64, 32, 3, 1, "vertical-ccw"
        )
        source = main.Image.new("RGB", (96, 64), (0, 0, 0))
        source.putpixel((0, 0), (255, 0, 0))

        display.show(source, 70)

        physical = physical_display.images[-1][0]
        self.assertEqual(physical.getpixel((0, 31)), (255, 0, 0))


class MatrixRendererColorTests(unittest.TestCase):
    class FakeDisplay:
        def __init__(self, width, height):
            self.width = width
            self.height = height
            self.images = []

        def show(self, image, brightness):
            self.images.append((image.copy(), brightness))

    def _renderer_with_colors(self, width=192, height=32, inning_half="top"):
        state = main.ScoreboardState(inning_half=inning_half)
        state.text_colors.update(
            {
                "team_a_name": "#112233",
                "team_b_name": "#445566",
                "inning_value": "#AABBCC",
            }
        )
        state.clamp()
        return main.MatrixRenderer(self.FakeDisplay(width, height), state)

    def test_top_indicator_uses_inning_value_color_in_horizontal_layout(self):
        renderer = self._renderer_with_colors(inning_half="top")

        with mock.patch.object(renderer, "_draw_inning_line") as draw_inning_line:
            renderer.draw_mode()

        self.assertEqual(draw_inning_line.call_args.args[4], "TOP")
        self.assertEqual(draw_inning_line.call_args.args[6], (170, 187, 204))
        self.assertNotEqual(draw_inning_line.call_args.args[6], (17, 34, 51))

    def test_bottom_indicator_uses_inning_value_color_in_vertical_layout(self):
        renderer = self._renderer_with_colors(width=64, height=96, inning_half="bottom")

        with mock.patch.object(renderer, "_draw_inning_line") as draw_inning_line:
            renderer.draw_mode()

        self.assertEqual(draw_inning_line.call_args.args[4], "BOT")
        self.assertEqual(draw_inning_line.call_args.args[6], (170, 187, 204))
        self.assertNotEqual(draw_inning_line.call_args.args[6], (68, 85, 102))

    def test_inning_line_starts_with_half_and_inning_without_label(self):
        renderer = self._renderer_with_colors()
        image = main.Image.new("RGB", (64, 32), (0, 0, 0))
        draw = main.ImageDraw.Draw(image)

        with (
            mock.patch.object(
                renderer, "_draw_scaled_text", wraps=renderer._draw_scaled_text
            ) as draw_scaled_text,
            mock.patch.object(
                renderer, "_draw_inning_number", wraps=renderer._draw_inning_number
            ) as draw_inning_number,
            mock.patch.object(draw, "text", wraps=draw.text) as draw_text,
        ):
            renderer._draw_inning_line(
                draw,
                2,
                1,
                "7",
                "TOP",
                (170, 187, 204),
                (170, 187, 204),
            )

        draw_scaled_text.assert_not_called()
        self.assertEqual(draw_text.call_args.args[1], "TOP ")
        self.assertEqual(draw_text.call_args.args[0][0], 2)
        self.assertEqual(draw_text.call_args.args[0][1], 2)
        self.assertEqual(draw_inning_number.call_args.args[1][1], 1)
        self.assertEqual(draw_inning_number.call_args.args[2], "7")
        self.assertLess(
            draw_text.call_args.args[0][0],
            draw_inning_number.call_args.args[1][0],
        )

    def test_team_name_font_uses_native_bitmap_size(self):
        renderer = self._renderer_with_colors()

        self.assertEqual(renderer._team_name_size("AWAY TEAM"), (45, 8))
        self.assertLess(
            renderer._team_name_size("AWAY TEAM")[0], renderer._text_width("AWAY TEAM")
        )

    def test_horizontal_team_name_uses_dedicated_font_without_resampling(self):
        renderer = self._renderer_with_colors()

        with mock.patch.object(renderer, "_draw_team_name") as draw_team_name:
            renderer.draw_mode()

        self.assertEqual(
            draw_team_name.call_args_list[0].args[2], renderer.state.team_a
        )
        self.assertEqual(
            draw_team_name.call_args_list[1].args[2], renderer.state.team_b
        )

    def test_horizontal_batting_order_is_drawn_at_screen_bottom(self):
        renderer = self._renderer_with_colors()

        with mock.patch.object(renderer, "_draw_batting_order") as draw_batting_order:
            renderer.draw_mode()

        first_call = draw_batting_order.call_args_list[0].args
        self.assertEqual(first_call[2], renderer.display.height - 1)

    def test_vertical_batting_order_is_drawn_at_team_panel_bottom(self):
        renderer = self._renderer_with_colors(width=64, height=96)

        with mock.patch.object(renderer, "_draw_batting_order") as draw_batting_order:
            renderer.draw_mode()

        first_call = draw_batting_order.call_args_list[0].args
        self.assertEqual(first_call[2], 31)

    def test_score_uses_large_seven_segment_digit_size(self):
        renderer = self._renderer_with_colors()

        self.assertEqual(renderer._score_text_size("9"), (14, 24))
        self.assertEqual(renderer._score_text_size("99"), (30, 24))

    def test_score_draw_uses_seven_segment_score_text_not_generic_scaled_text(self):
        renderer = self._renderer_with_colors()
        renderer.state.score_a = 12
        renderer.state.score_b = 9

        with (
            mock.patch.object(
                renderer, "_draw_scaled_text", wraps=renderer._draw_scaled_text
            ) as draw_scaled_text,
            mock.patch.object(
                renderer, "_draw_score_text", wraps=renderer._draw_score_text
            ) as draw_score_text,
            mock.patch.object(
                renderer,
                "_draw_seven_segment_text",
                wraps=renderer._draw_seven_segment_text,
            ) as draw_seven_segment_text,
        ):
            renderer.draw_mode()

        scaled_text_values = [call.args[2] for call in draw_scaled_text.call_args_list]
        score_text_values = [call.args[2] for call in draw_score_text.call_args_list]
        seven_segment_values = [
            call.args[2] for call in draw_seven_segment_text.call_args_list
        ]
        self.assertNotIn("12", scaled_text_values)
        self.assertNotIn("9", scaled_text_values)
        self.assertEqual(score_text_values, ["12", "9"])
        self.assertIn("12", seven_segment_values)
        self.assertIn("9", seven_segment_values)

    def test_score_digit_one_matches_other_digits_height_without_overflow(self):
        renderer = self._renderer_with_colors()
        image = main.Image.new("RGB", (24, 32), (0, 0, 0))
        draw = main.ImageDraw.Draw(image)
        active_color = (255, 180, 0)

        renderer._draw_score_text(draw, (3, 3), "1", active_color)

        pixels = image.load()
        right_segment_x = 3 + main.SEVEN_SEGMENT_SCORE_DIGIT_SIZE[0] - 1
        digit_top = 3
        digit_bottom = digit_top + main.SEVEN_SEGMENT_SCORE_DIGIT_SIZE[1] - 1
        self.assertEqual(pixels[right_segment_x, digit_top], active_color)
        self.assertEqual(pixels[right_segment_x, digit_top + 1], active_color)
        self.assertEqual(pixels[right_segment_x, digit_bottom - 1], active_color)
        self.assertEqual(pixels[right_segment_x, digit_bottom], active_color)
        self.assertEqual(pixels[right_segment_x, digit_top - 1], (0, 0, 0))
        self.assertEqual(pixels[right_segment_x, digit_bottom + 1], (0, 0, 0))

    def test_inning_digit_one_matches_other_digits_height_without_overflow(self):
        renderer = self._renderer_with_colors()
        image = main.Image.new("RGB", (16, 24), (0, 0, 0))
        draw = main.ImageDraw.Draw(image)
        active_color = (170, 187, 204)

        renderer._draw_inning_number(draw, (4, 4), "1", active_color)

        pixels = image.load()
        right_segment_x = 4 + main.SEVEN_SEGMENT_INNING_DIGIT_SIZE[0] - 1
        digit_top = 4
        digit_bottom = digit_top + main.SEVEN_SEGMENT_INNING_DIGIT_SIZE[1] - 1
        self.assertEqual(pixels[right_segment_x, digit_top], active_color)
        self.assertEqual(pixels[right_segment_x, digit_bottom], active_color)
        self.assertEqual(pixels[right_segment_x, digit_top - 1], (0, 0, 0))
        self.assertEqual(pixels[right_segment_x, digit_bottom + 1], (0, 0, 0))

    def test_count_indicators_are_three_by_three_squares(self):
        renderer = self._renderer_with_colors()
        image = main.Image.new("RGB", (24, 12), (0, 0, 0))
        draw = main.ImageDraw.Draw(image)
        active_color = (255, 50, 50)

        renderer._draw_count_dots(
            draw,
            0,
            6,
            "B",
            1,
            1,
            (255, 255, 255),
            active_color,
            (48, 48, 48),
        )

        pixels = image.load()
        active_pixels = sum(
            1
            for x in range(image.width)
            for y in range(image.height)
            if pixels[x, y] == active_color
        )
        self.assertEqual(active_pixels, 9)

    def test_vertical_screen_team_panels_place_name_score_and_batting_order(self):
        renderer = self._renderer_with_colors(width=96, height=64)

        with (
            mock.patch.object(renderer, "_draw_clipped_team_name") as draw_team_name,
            mock.patch.object(renderer, "_draw_score_text") as draw_score_text,
            mock.patch.object(renderer, "_draw_batting_order") as draw_batting_order,
        ):
            renderer.draw_mode()

        self.assertEqual(draw_team_name.call_args_list[0].args[1], (2, 0))
        self.assertEqual(draw_team_name.call_args_list[1].args[1], (34, 0))
        self.assertEqual(draw_score_text.call_args_list[0].args[1][1], 20)
        self.assertEqual(draw_score_text.call_args_list[1].args[1][1], 20)
        self.assertEqual(draw_batting_order.call_args_list[0].args[2], 63)
        self.assertEqual(draw_batting_order.call_args_list[1].args[2], 63)

    def test_vertical_screen_info_panel_places_inning_at_top_and_stacks_counts(self):
        renderer = self._renderer_with_colors(width=96, height=64)

        with (
            mock.patch.object(renderer, "_draw_inning_line") as draw_inning_line,
            mock.patch.object(renderer, "_draw_count_dots") as draw_count_dots,
        ):
            renderer.draw_mode()

        self.assertEqual(draw_inning_line.call_args.args[1:5], (66, 0, "1", "TOP"))
        count_positions = [
            (call.args[1], call.args[2], call.args[3])
            for call in draw_count_dots.call_args_list
        ]
        self.assertEqual(count_positions, [(66, 27, "B"), (66, 41, "S"), (66, 55, "O")])

    def test_two_panel_top_places_counts_on_panel_one_and_inning_on_panel_two(self):
        renderer = self._renderer_with_colors(width=128, height=32, inning_half="top")
        renderer.two_panel_layout = True

        with (
            mock.patch.object(renderer, "_draw_count_dots") as draw_count_dots,
            mock.patch.object(renderer, "_draw_inning_number") as draw_inning_number,
        ):
            renderer.draw_mode()

        count_positions = [
            (call.args[1], call.args[2], call.args[3])
            for call in draw_count_dots.call_args_list
        ]
        self.assertEqual(count_positions, [(2, 28, "B"), (25, 28, "S"), (44, 28, "O")])
        self.assertEqual(draw_inning_number.call_args.args[1], (92, 18))

    def test_two_panel_bottom_places_counts_on_panel_two_and_inning_on_panel_one(self):
        renderer = self._renderer_with_colors(
            width=128, height=32, inning_half="bottom"
        )
        renderer.two_panel_layout = True

        with (
            mock.patch.object(renderer, "_draw_count_dots") as draw_count_dots,
            mock.patch.object(renderer, "_draw_inning_number") as draw_inning_number,
        ):
            renderer.draw_mode()

        count_positions = [
            (call.args[1], call.args[2], call.args[3])
            for call in draw_count_dots.call_args_list
        ]
        self.assertEqual(
            count_positions, [(66, 28, "B"), (89, 28, "S"), (108, 28, "O")]
        )
        self.assertEqual(draw_inning_number.call_args.args[1], (28, 18))

    def test_two_panel_layout_keeps_team_panels_on_ports_one_and_two(self):
        renderer = self._renderer_with_colors(width=128, height=32, inning_half="top")
        renderer.two_panel_layout = True

        with mock.patch.object(renderer, "_draw_team_panel") as draw_team_panel:
            renderer.draw_mode()

        panel_origins = [call.args[1] for call in draw_team_panel.call_args_list]
        self.assertEqual(panel_origins, [0, 64])

    def test_two_panel_vertical_top_places_stacked_counts_and_inning(self):
        renderer = self._renderer_with_colors(width=64, height=64, inning_half="top")
        renderer.two_panel_layout = True

        with (
            mock.patch.object(renderer, "_draw_count_dots") as draw_count_dots,
            mock.patch.object(renderer, "_draw_inning_number") as draw_inning_number,
        ):
            renderer.draw_mode()

        count_positions = [
            (call.args[1], call.args[2], call.args[3])
            for call in draw_count_dots.call_args_list
        ]
        self.assertEqual(count_positions, [(2, 15, "B"), (2, 23, "S"), (2, 31, "O")])
        self.assertEqual(draw_inning_number.call_args.args[1], (44, 14))

    def test_two_panel_vertical_bottom_swaps_stacked_counts_and_inning(self):
        renderer = self._renderer_with_colors(width=64, height=64, inning_half="bottom")
        renderer.two_panel_layout = True

        with (
            mock.patch.object(renderer, "_draw_count_dots") as draw_count_dots,
            mock.patch.object(renderer, "_draw_inning_number") as draw_inning_number,
        ):
            renderer.draw_mode()

        count_positions = [
            (call.args[1], call.args[2], call.args[3])
            for call in draw_count_dots.call_args_list
        ]
        self.assertEqual(
            count_positions, [(34, 15, "B"), (34, 23, "S"), (34, 31, "O")]
        )
        self.assertEqual(draw_inning_number.call_args.args[1], (12, 14))

    def test_two_panel_vertical_keeps_team_panels_on_rotated_ports_one_and_two(self):
        renderer = self._renderer_with_colors(width=64, height=64, inning_half="top")
        renderer.two_panel_layout = True

        with mock.patch.object(
            renderer, "_draw_two_panel_vertical_team_panel"
        ) as draw_team_panel:
            renderer.draw_mode()

        panel_origins = [call.args[1] for call in draw_team_panel.call_args_list]
        self.assertEqual(panel_origins, [0, 32])


class ScoreboardStateLimitTests(unittest.TestCase):
    def test_inning_clamps_to_twenty(self):
        state = main.ScoreboardState(inning=25)
        state.clamp()

        self.assertEqual(state.inning, 20)

    def test_inning_increment_stops_at_twenty(self):
        state = main.ScoreboardState(inning=20)
        state.update("inning_inc")

        self.assertEqual(state.inning, 20)


class ConfigRouteTests(unittest.TestCase):
    class FakeRenderer:
        def __init__(self):
            self.draw_calls = 0

        def draw(self):
            self.draw_calls += 1

    def test_brightness_route_updates_slider_without_config_save(self):
        state = main.ScoreboardState(brightness=70)
        renderer = self.FakeRenderer()
        app = main.create_app(state, renderer)

        with mock.patch.object(main, "save_state") as save_state:
            response = app.test_client().post("/brightness", data={"brightness": "42"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"brightness": 42})
        self.assertEqual(state.brightness, 42)
        self.assertEqual(renderer.draw_calls, 1)
        save_state.assert_called_once_with(state)

    def test_brightness_route_clamps_slider_value(self):
        state = main.ScoreboardState(brightness=70)
        renderer = self.FakeRenderer()
        app = main.create_app(state, renderer)

        with mock.patch.object(main, "save_state"):
            response = app.test_client().post("/brightness", data={"brightness": "101"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"brightness": 100})
        self.assertEqual(state.brightness, 100)
        self.assertEqual(renderer.draw_calls, 1)

    def test_config_updates_brightness_from_slider(self):
        state = main.ScoreboardState(brightness=70)
        renderer = self.FakeRenderer()
        app = main.create_app(state, renderer)

        payload = {key: value for key, value in state.text_colors.items()}
        payload.update(
            {
                "brightness": "42",
                "batting_order_enabled": "1",
                "batting_order_a": str(state.batting_order_a),
                "batting_order_b": str(state.batting_order_b),
            }
        )
        with mock.patch.object(main, "save_state"):
            response = app.test_client().post("/config", data=payload)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(state.brightness, 42)
        self.assertEqual(renderer.draw_calls, 1)


if __name__ == "__main__":
    unittest.main()
