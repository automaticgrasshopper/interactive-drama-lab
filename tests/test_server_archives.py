import subprocess
import unittest
from unittest.mock import patch

from backend import server


class GitArchiveAggregationTests(unittest.TestCase):
    def test_script_and_storyboard_with_different_times_share_one_record(self):
        script_path = "h5/datapacks/同一作品.script.json"
        storyboard_path = "h5/datapacks/同一作品.sb.json"
        payloads = {
            script_path: {"title": "同一作品", "time": "2026/7/10 19:38:04"},
            storyboard_path: {
                "title": "同一作品",
                "boundStory": "同一作品",
                "time": "2026/7/13 12:06:36",
            },
        }

        def tree_paths(_ref, prefix):
            return list(payloads) if prefix == "h5/datapacks" else []

        no_upstream = subprocess.CompletedProcess([], 1, "", "")
        with (
            patch.object(server, "run_git", return_value=no_upstream),
            patch.object(server, "git_tree_paths", side_effect=tree_paths),
            patch.object(server, "git_blob_json", side_effect=lambda _ref, path: payloads[path]),
        ):
            records = server.git_archived_tasks()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "同一作品")
        self.assertTrue(records[0]["has_cg"])
        self.assertEqual(records[0]["phase"], "CG / 分镜归档")
        self.assertEqual(records[0]["created_at"], "2026-07-10T19:38:04")
        self.assertEqual(records[0]["updated_at"], "2026-07-13T12:06:36")
        self.assertCountEqual(records[0]["source_paths"], [script_path, storyboard_path])


if __name__ == "__main__":
    unittest.main()
