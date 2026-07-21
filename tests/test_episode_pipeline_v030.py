from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.production_worker import ProductionManager, REQUIRED_BACKEND_REFERENCE_PHASES, script_digest


ROOT = Path(__file__).resolve().parents[1]


class EpisodePipelineV037Test(unittest.TestCase):
    def test_manifest_and_skill_are_v037(self):
        manifest = json.loads((ROOT / "skill" / "episode-generator" / "reference-manifest.json").read_text(encoding="utf-8"))
        skill = (ROOT / "skill" / "episode-generator" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["skill_version"], "v0.1.37")
        self.assertIn("当前规范版本：`v0.1.37`", skill)
        self.assertIn("# 分集规划师", skill)
        self.assertNotIn("情绪脊分集规划师", skill)
        self.assertNotIn("对白情绪词正负分", skill)

    def test_emotional_spine_reference_uses_formal_v037_language(self):
        reference = (ROOT / "skill" / "episode-generator" / "references" / "emotional-spine-state-graph.md").read_text(encoding="utf-8")
        self.assertIn("## 三、情绪脊逻辑", reference)
        self.assertIn("V（Valence）", reference)
        self.assertNotIn("情绪脊替代情绪分数", reference)
        self.assertNotIn("喜悦为正", reference)
        self.assertNotIn("InkOS", reference)

    def test_story_rhythm_platform_is_aligned_to_v037(self):
        platform = (ROOT / "h5" / "影视互动游戏故事节奏验证.html").read_text(encoding="utf-8")
        self.assertIn("episode-generator v0.1.37 分集规划师", platform)
        self.assertIn("0.1.37-workbench", platform)
        self.assertNotIn("episode-generator v0.1.36", platform)
        self.assertNotIn("0.1.36-workbench", platform)

    def test_backend_runtime_has_required_dependency_phases(self):
        self.assertEqual(
            REQUIRED_BACKEND_REFERENCE_PHASES,
            {"upstream", "topology", "episode-writing", "dialogue-polish", "continuity-review"},
        )

    def test_every_topology_node_normalizes_to_one_episode(self):
        data = {
            "nodes": [
                {"id": "choice", "node_title": "选择", "next": [{"to": "a", "label": "公开"}, {"to": "b", "label": "隐瞒"}]},
                {"id": "a", "node_title": "公开结果", "next": []},
                {"id": "b", "node_title": "隐瞒结果", "next": []},
            ],
            "episode_map": [{"id": "wrong"}],
        }
        ProductionManager._normalize_episode_nodes(data)
        self.assertEqual([node["id"] for node in data["nodes"]], ["episode-001", "episode-002", "episode-003"])
        self.assertEqual([edge["to"] for edge in data["nodes"][0]["next"]], ["episode-002", "episode-003"])
        self.assertNotIn("episode_map", data)

    def test_lightweight_lock_only_depends_on_polished_current_script(self):
        script = "【场一 · 旧屋 · 夜 · 内】\n出场：甲\n\n甲：行，先这么办。"
        node = {
            "script": script,
            "lightweight_status": "已口语化",
            "dialogue_polished_digest": script_digest(script),
        }
        self.assertTrue(ProductionManager._is_locked(node))
        node["script"] += "\n乙：等等。"
        self.assertFalse(ProductionManager._is_locked(node))

    def test_outline_check_no_longer_requires_emotion_or_production_cards(self):
        data = {
            "characters": [{"name": "甲", "role": "主角"}],
            "scenes": [{"name": "旧屋"}],
            "props": [{"name": "旧电脑"}],
            "outline": [{"summary": "甲为了拿到第一笔收入修理旧电脑，受到文件损坏阻碍，最终找回资料并获得下一笔生意机会。"}],
            "nodes": [
                {"id": "1", "kind": "normal", "text": "甲为了拿到第一笔收入修理旧电脑，受到文件损坏阻碍，最终找回资料并得到下一笔生意机会。", "next": [{"to": "E"}]},
                {"id": "E", "kind": "major", "text": "甲完成第一笔生意，决定抓住新的网络创业机会，同时发现电脑时间发生异常。", "next": []},
            ],
        }
        self.assertEqual(ProductionManager._outline_errors(data, 10, 1), [])


if __name__ == "__main__":
    unittest.main()
