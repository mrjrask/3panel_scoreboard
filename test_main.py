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


if __name__ == "__main__":
    unittest.main()
