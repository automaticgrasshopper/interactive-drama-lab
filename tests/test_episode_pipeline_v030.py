from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.production_worker import ProductionManager, REQUIRED_BACKEND_REFERENCE_PHASES, script_digest


ROOT = Path(__file__).resolve().parents[1]


class EpisodePipelineV047Test(unittest.TestCase):
    def test_story_rhythm_platform_is_decoupled_from_skill(self):
        platform = (ROOT / "h5" / "影视互动游戏故事节奏验证.html").read_text(encoding="utf-8")
        self.assertNotIn("episode-generator v0.1.47 分集规划师", platform)
        self.assertNotIn("分集 Skill 读取中", platform)
        self.assertNotIn("episodeSkillVersion", platform)
        self.assertIn("beats ≥ 5 拍", platform)
        self.assertIn("人话台词复写", platform)

    def test_backend_runtime_has_required_dependency_phases(self):
        self.assertEqual(
            REQUIRED_BACKEND_REFERENCE_PHASES,
            {"upstream", "topology", "episode-writing", "dialogue-polish", "episode-quality-review"},
        )

    def test_backend_v047_uses_one_compact_postwriting_quality_gate(self):
        worker = (ROOT / "backend" / "production_worker.py").read_text(encoding="utf-8")
        self.assertIn('"pipeline": "episode-writing → dialogue-polish → episode-quality-review"', worker)
        self.assertIn('reference_phase="episode-quality-review"', worker)
        self.assertNotIn("_continuity_only_review", worker)
        self.assertNotIn("length_anchor", worker)
        self.assertNotIn("90%—110%", worker)

    def test_written_text_reference_is_loaded_only_for_matching_current_node(self):
        data = {"synopsis": "主角曾收到一封信。"}
        plain_node = {"id": "episode-001", "text": "主角当面追问朋友。"}
        text_node = {"id": "episode-002", "text": "主角打开短信，内容改变了行动。"}
        self.assertNotIn("written-text", ProductionManager._reference_profiles(data, plain_node))
        self.assertIn("written-text", ProductionManager._reference_profiles(data, text_node))

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

    def test_lock_requires_quality_review_bound_to_current_script(self):
        script = "【场一 · 旧屋 · 夜 · 内】\n出场：甲\n\n甲：行，先这么办。"
        node = {
            "script": script,
            "lightweight_status": "已复检",
            "dialogue_polished_digest": script_digest(script),
            "episode_audit": {"reviews": [{"name": "quality", "pass": True, "digest": script_digest(script)}]},
        }
        self.assertTrue(ProductionManager._is_locked(node))
        node["script"] += "\n乙：等等。"
        self.assertFalse(ProductionManager._is_locked(node))

        legacy = {
            "script": script,
            "episode_audit": {
                "locked": True,
                "digest": script_digest(script),
                "reviews": [
                    {"name": name, "pass": True, "digest": script_digest(script)}
                    for name in ("mechanical", "causal", "dialogue", "cold")
                ],
            },
        }
        self.assertFalse(ProductionManager._is_locked(legacy))

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
