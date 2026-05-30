import errno
import json
import tempfile
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
        with mock.patch.object(
            main.os,
            "fsync",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), self.assertLogs(main.LOGGER, level="ERROR") as logs:
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
        with mock.patch.object(
            main.tempfile,
            "mkstemp",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), self.assertLogs(main.LOGGER, level="ERROR") as logs:
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
            mock.patch.object(
                main.tempfile, "mkstemp", side_effect=fail_primary_only
            ),
            self.assertLogs(main.LOGGER, level="WARNING") as logs,
        ):
            main.save_state(state)

        fallback_file = fallback_home / "3panel_scoreboard" / "scoreboard_state.json"
        saved = json.loads(fallback_file.read_text())
        self.assertEqual(saved["score_a"], 11)
        self.assertEqual(saved["score_b"], 10)
        self.assertEqual(main.STATE_FILE, fallback_file)
        self.assertIn("Saved this and future changes", "\n".join(logs.output))


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
                "inning_label": "#778899",
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
        self.assertEqual(draw_inning_line.call_args.args[7], (170, 187, 204))
        self.assertNotEqual(draw_inning_line.call_args.args[7], (17, 34, 51))

    def test_bottom_indicator_uses_inning_value_color_in_vertical_layout(self):
        renderer = self._renderer_with_colors(width=64, height=96, inning_half="bottom")

        with mock.patch.object(renderer, "_draw_inning_line") as draw_inning_line:
            renderer.draw_mode()

        self.assertEqual(draw_inning_line.call_args.args[4], "BOT")
        self.assertEqual(draw_inning_line.call_args.args[7], (170, 187, 204))
        self.assertNotEqual(draw_inning_line.call_args.args[7], (68, 85, 102))

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

    def test_score_uses_double_native_score_font_size(self):
        renderer = self._renderer_with_colors()
        score_bbox = renderer.score_font.getbbox("99")
        native_size = (score_bbox[2] - score_bbox[0], score_bbox[3] - score_bbox[1])

        self.assertEqual(
            renderer._score_text_size("99"),
            (native_size[0] * main.SCORE_SCALE, native_size[1] * main.SCORE_SCALE),
        )
        self.assertEqual(renderer._score_text_size("99"), (36, 30))

    def test_score_draw_uses_doubled_score_text_not_generic_scaled_text(self):
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
        ):
            renderer.draw_mode()

        scaled_text_values = [call.args[2] for call in draw_scaled_text.call_args_list]
        score_text_values = [call.args[2] for call in draw_score_text.call_args_list]
        self.assertNotIn("12", scaled_text_values)
        self.assertNotIn("9", scaled_text_values)
        self.assertEqual(score_text_values, ["12", "9"])


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
