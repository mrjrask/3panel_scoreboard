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


class WebUITests(unittest.TestCase):
    def test_sections_are_collapsible_with_layout_colors_closed_by_default(self):
        state = main.ScoreboardState()
        renderer = mock.Mock()
        app = main.create_app(state, renderer)

        response = app.test_client().get("/")
        html = response.get_data(as_text=True)

        self.assertIn("<details class='card' data-section='score' open>", html)
        self.assertIn("<details class='card' data-section='teams' open>", html)
        self.assertIn("<details class='card' data-section='layout-colors'>", html)
        self.assertIn("<summary>Layout & Colors</summary>", html)
        self.assertIn("<details class='card' data-section='controls' open>", html)

    def test_web_ui_submits_buttons_without_full_page_navigation(self):
        state = main.ScoreboardState()
        renderer = mock.Mock()
        app = main.create_app(state, renderer)

        response = app.test_client().get("/")
        html = response.get_data(as_text=True)

        self.assertIn("event.preventDefault();", html)
        self.assertIn("window.scrollTo(scrollPosition.x, scrollPosition.y);", html)
        self.assertIn("fetch(form.action", html)


if __name__ == "__main__":
    unittest.main()
