from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / "backend" / "execution" / "scripts"
GATE_PATH = SCRIPT_ROOT / "episode_quality_gate.py"
ASSEMBLER_PATH = SCRIPT_ROOT / "validate_and_assemble_scripts.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("episode_quality_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_assembler():
    sys.path.insert(0, str(SCRIPT_ROOT))
    try:
        spec = importlib.util.spec_from_file_location("validate_and_assemble_scripts", ASSEMBLER_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


EPISODE = """# 分集编号

episode-001

# 分集标题

开场

# 分集剧本

## 单集梗概

甲进门。

## 完整剧本

【旧屋·夜·内】

甲推开门，看见桌上的灯仍亮着。

甲：我回来了。

# 剧本分析

## 本集冲突

甲确认屋内是否有人。

## 前置节点编号列表

无

## 后续节点编号列表

无

# 关联角色

甲

# 关联场景

旧屋

# 关联道具

旧灯

# 是否结局

是

# 互动节点

## 是否为分支节点

否

## 是否有选择问题

否

## 选择问题

无

## 选项列表

无

## 默认下一分集编号

无
"""


class EpisodeQualityGateTests(unittest.TestCase):
    def project(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "episodes").mkdir()
        (root / "episodes" / "episode-001.md").write_text(EPISODE, encoding="utf-8")
        (root / "assets.json").write_text(json.dumps({"characters": ["甲"], "scenes": ["旧屋"], "props": ["旧灯"]}, ensure_ascii=False), encoding="utf-8")
        (root / "topology.md").write_text("## episode-001｜开场\n- 互动类型：正式结局\n- 结局：是\n- 后续节点：无\n", encoding="utf-8")
        return root

    def valid_review(self, packet):
        evidence = []
        for check in packet["required_checks"]:
            if check == "普通话表达与叙述可读":
                evidence.append({
                    "check": check,
                    "dialogue_location": "甲：我回来了。",
                    "dialogue_proof": "台词采用自然普通话语序并回应现场。",
                    "narration_location": "甲推开门",
                    "narration_proof": "描述写清人物、动作、对象和现场变化。",
                })
            else:
                evidence.append({"check": check, "location": "【旧屋·夜·内】", "proof": "已逐场核对当前正文中的具体证据。"})
        return {
            "script_sha256": packet["script_sha256"],
            "reference_sha256": packet["reference_sha256"],
            "comprehension": {
                "character_task": {"answer": "甲回到旧屋确认屋内情况。", "proof": "正文写明甲推开门并查看仍亮着的灯。"},
                "trigger_cost": {"answer": "屋内的灯仍亮着，意味着可能有人。", "proof": "甲进门后首先看见桌上的灯仍亮着。"},
                "action_result": {"answer": "甲推门进入并出声确认自己回来。", "proof": "正文连续写出推门、看灯和说我回来了。"},
                "next_entry": {"answer": "甲等待屋内的人或异常作出回应。", "proof": "结尾停在甲向屋内出声之后，形成等待回应的压力。"},
            },
            "covered_checks": list(packet["required_checks"]),
            "issues": [],
            "evidence": evidence,
            "note": "独立复检完成",
        }

    def test_packet_contains_current_reference_and_script(self):
        gate = load_gate()
        packet = gate.build_packet(self.project(), "episode-001")
        self.assertIn("七项完整覆盖", packet["reference"])
        self.assertIn("甲：我回来了。", packet["script"])
        self.assertNotIn("甲确认屋内是否有人", packet["script"])
        self.assertNotIn("后续节点编号列表", packet["script"])
        self.assertEqual(packet["execution_schema_version"], "v1")

    def test_comprehension_gate_requires_all_four_answers_and_proofs(self):
        gate = load_gate()
        project = self.project()
        packet = gate.build_packet(project, "episode-001")
        review = self.valid_review(packet)
        review["comprehension"]["trigger_cost"].pop("proof")
        review["comprehension"]["next_entry"]["proof"] = "正文不清楚，无法提供依据。"
        errors = gate.validate_review(packet, review)
        self.assertTrue(any("触发代价" in error for error in errors))
        self.assertTrue(any("下一入口" in error for error in errors))

    def test_missing_receipt_blocks_next_stage(self):
        gate = load_gate()
        errors = gate.verify_project(self.project())
        self.assertTrue(any("缺少当前有效" in error for error in errors))

    def test_first_appearance_check_is_mandatory(self):
        gate = load_gate()
        project = self.project()
        packet = gate.build_packet(project, "episode-001")
        review = self.valid_review(packet)
        review["covered_checks"].remove("首次出现与必要交代")
        review["evidence"] = [item for item in review["evidence"] if item["check"] != "首次出现与必要交代"]
        self.assertTrue(any("首次出现与必要交代" in error for error in gate.validate_review(packet, review)))

    def test_plain_mandarin_check_requires_dialogue_and_narration_evidence(self):
        gate = load_gate()
        project = self.project()
        packet = gate.build_packet(project, "episode-001")
        review = self.valid_review(packet)
        plain = next(item for item in review["evidence"] if item["check"] == "普通话表达与叙述可读")
        plain.pop("narration_proof")
        self.assertTrue(any("台词与描述必须分别举证" in error for error in gate.validate_review(packet, review)))

    def test_changed_script_invalidates_sealed_receipt(self):
        gate = load_gate()
        project = self.project()
        packet = gate.build_packet(project, "episode-001")
        review_path = project / "review.json"
        review_path.write_text(json.dumps(self.valid_review(packet), ensure_ascii=False), encoding="utf-8")
        gate.seal(project, "episode-001", review_path)
        self.assertEqual(gate.verify_project(project), [])
        episode = project / "episodes" / "episode-001.md"
        episode.write_text(EPISODE.replace("我回来了", "我先回来了"), encoding="utf-8")
        self.assertTrue(any("已失效" in error or "证据无效" in error for error in gate.verify_project(project)))

    def test_assembler_refuses_unreviewed_episode_and_accepts_sealed_one(self):
        gate = load_gate()
        project = self.project()
        output = project / "episode-script.md"
        blocked = subprocess.run([sys.executable, str(ASSEMBLER_PATH), str(project), str(output)], capture_output=True, text=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("缺少当前有效的逐集复检回执", blocked.stdout)
        packet = gate.build_packet(project, "episode-001")
        review_path = project / "review.json"
        review_path.write_text(json.dumps(self.valid_review(packet), ensure_ascii=False), encoding="utf-8")
        gate.seal(project, "episode-001", review_path)
        passed = subprocess.run([sys.executable, str(ASSEMBLER_PATH), str(project), str(output)], capture_output=True, text=True)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertTrue(output.is_file())

    def test_nine_field_contract_rejects_topology_drift(self):
        assembler = load_assembler()
        node = {
            "title": "开场",
            "ending": False,
            "successors": ["episode-002"],
            "choices": [("打开门", "episode-002")],
            "question": "要打开门吗？",
        }
        choice_episode = (
            EPISODE
            .replace("## 后续节点编号列表\n\n无", "## 后续节点编号列表\n\nepisode-002")
            .replace("# 是否结局\n\n是", "# 是否结局\n\n否")
            .replace("# 关联角色\n\n甲", "# 关联角色\n\n无")
            .replace("# 关联场景\n\n旧屋", "# 关联场景\n\n无")
            .replace("# 关联道具\n\n旧灯", "# 关联道具\n\n无")
            .replace("## 是否为分支节点\n\n否", "## 是否为分支节点\n\n是")
            .replace("## 是否有选择问题\n\n否", "## 是否有选择问题\n\n是")
            .replace("## 选择问题\n\n无", "## 选择问题\n\n要打开门吗？")
            .replace(
                "## 选项列表\n\n无",
                "## 选项列表\n\n- 选项编号：1\n  - 选项文字：打开门\n  - 目标分集编号：episode-002",
            )
            .replace("## 默认下一分集编号\n\n无", "## 默认下一分集编号\n\nepisode-002")
        )
        self.assertEqual(assembler.validate_episode("episode-001", node, choice_episode, []), [])
        drifted = choice_episode.replace("选项文字：打开门", "选项文字：推开门").replace(
            "## 默认下一分集编号\n\nepisode-002",
            "## 默认下一分集编号\n\nepisode-999",
        )
        issues = assembler.validate_episode("episode-001", node, drifted, [])
        self.assertTrue(any("选项文字错误" in issue for issue in issues))
        self.assertTrue(any("默认下一分集错误" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
