from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skill" / "episode-generator" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from episode_task_state import claim, complete, planning_tasks, production_tasks  # noqa: E402


class EpisodePipelineV029Test(unittest.TestCase):
    def test_one_running_task_and_resume_returns_same_atom(self):
        ledger = {"tasks": planning_tasks()}
        first = claim(ledger)
        self.assertEqual(first["id"], "upstream-translate")
        self.assertIs(claim(ledger), first)
        self.assertEqual(sum(item["status"] == "running" for item in ledger["tasks"]), 1)

    def test_reviewer_cannot_self_sign_pass_without_coverage(self):
        ledger = {"tasks": planning_tasks()}
        current = claim(ledger)
        self.assertEqual(current["id"], "upstream-translate")
        complete(ledger, current["id"], {"output": current["id"]})
        current = claim(ledger)
        self.assertEqual(current["id"], "topology-write")
        complete(ledger, current["id"], {"output": current["id"]})
        review = claim(ledger)
        complete(ledger, review["id"], {"pass": True, "issues": []})
        self.assertIn(":fix-", claim(ledger)["id"])

    def test_each_flow_node_is_the_episode_task_scope(self):
        plan = {
            "episodes": [
                {"id": "episode-001", "next": [{"to": "episode-002"}]},
                {"id": "episode-002", "next": []},
            ]
        }
        tasks = production_tasks(plan)
        scopes = {item["scope_id"] for item in tasks if item["scope_id"].startswith("episode-")}
        self.assertEqual(scopes, {"episode-001", "episode-002"})
        self.assertNotIn("episode-map-write", {item["id"] for item in planning_tasks()})

    def test_unknown_episode_edge_is_rejected(self):
        plan = {"episodes": [{"id": "episode-001", "next": [{"to": "episode-999"}]}]}
        with self.assertRaisesRegex(ValueError, "未知分集"):
            production_tasks(plan)


if __name__ == "__main__":
    unittest.main()
