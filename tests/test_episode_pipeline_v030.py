from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.production_worker import ProductionManager, REQUIRED_BACKEND_REFERENCE_PHASES, script_digest


ROOT = Path(__file__).resolve().parents[1]


class EpisodePipelineV037Test(unittest.TestCase):
    def test_skill_package_contains_only_runtime_files(self):
        skill_root = ROOT / "skill" / "episode-generator"
        files = {
            path.relative_to(skill_root).as_posix()
            for path in skill_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(
            files,
            {
                "SKILL.md",
                "agents/openai.yaml",
                "reference-manifest.json",
                "references/causal-episode-writing.md",
                "references/chinese-dialogue-craft.md",
                "references/emotional-spine-state-graph.md",
                "references/public-output-and-progress.md",
                "references/upstream-input-translation.md",
                "scripts/validate_and_assemble_scripts_v036.py",
                "scripts/validate_and_assemble_scripts_v037.py",
                "scripts/validate_emotional_topology.py",
                "scripts/validate_topology.py",
            },
        )

    def test_manifest_and_skill_are_v037(self):
        manifest = json.loads((ROOT / "skill" / "episode-generator" / "reference-manifest.json").read_text(encoding="utf-8"))
        skill = (ROOT / "skill" / "episode-generator" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["skill_version"], "v0.1.37")
        self.assertNotIn("当前规范版本：", skill)
        self.assertIn("# 分集规划师", skill)
        self.assertNotIn("情绪脊分集规划师", skill)
        self.assertNotIn("对白情绪词正负分", skill)

    def test_emotional_spine_reference_matches_original_v01_topology(self):
        reference = (ROOT / "skill" / "episode-generator" / "references" / "emotional-spine-state-graph.md").read_text(encoding="utf-8")
        original = (ROOT / "skill-backups" / "episode-generator" / "versions" / "v0.1" / "references" / "emotional-spine-and-branching.md").read_text(encoding="utf-8")
        self.assertEqual(reference, original)
        self.assertIn("## 二、建立 PAD/VAD 情绪脊", reference)
        self.assertIn("## 三、从情绪脊识别天然岔点", reference)
        self.assertIn("## 四、分支开扇与编织带原则", reference)
        self.assertIn("### 带差异地合流", reference)
        self.assertIn("### 收扇", reference)

    def test_story_rhythm_platform_is_aligned_to_v037(self):
        platform = (ROOT / "h5" / "影视互动游戏故事节奏验证.html").read_text(encoding="utf-8")
        self.assertIn("episode-generator v0.1.37 分集规划师", platform)
        self.assertIn("0.1.37-workbench", platform)
        self.assertIn("人话台词校验", platform)
        self.assertIn("dialogue-polish-only", platform)
        self.assertNotIn("episode-generator v0.1.36", platform)
        self.assertNotIn("0.1.36-workbench", platform)
        self.assertNotIn("全剧压缩校验", platform)
        self.assertNotIn("single four-gate check", platform)
        self.assertNotIn("逐集制作卡完整", platform)
        self.assertNotIn("全部分集同摘要三审锁稿", platform)
        self.assertNotIn("存在 D 轴翻盘拍", platform)
        self.assertNotIn("beats ≥5 拍", platform)
        self.assertNotIn("释放点过密", platform)

    def test_backend_runtime_has_required_dependency_phases(self):
        self.assertEqual(
            REQUIRED_BACKEND_REFERENCE_PHASES,
            {"upstream", "topology", "episode-writing", "dialogue-polish"},
        )

    def test_backend_v037_has_no_postwriting_continuity_or_length_gate(self):
        worker = (ROOT / "backend" / "production_worker.py").read_text(encoding="utf-8")
        self.assertIn('"pipeline": "episode-writing → dialogue-polish"', worker)
        self.assertNotIn("_continuity_only_review", worker)
        self.assertNotIn("length_anchor", worker)
        self.assertNotIn("90%—110%", worker)

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
