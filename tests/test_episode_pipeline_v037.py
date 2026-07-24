from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / "backend" / "execution" / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class EmotionalSpineTopologyV037Test(unittest.TestCase):
    def test_original_emotional_spine_graph_does_not_require_state_fields(self):
        module = load_module("validate_emotional_topology", SCRIPT_ROOT / "validate_emotional_topology.py")
        nodes = []
        nodes.append(
            """## episode-001｜抉择
- 互动类型：关键选择
- 结局：否
- 后续节点：episode-002、episode-003、episode-004、episode-005
- 选择：推开东门 → episode-002
- 选择：推开西门 → episode-003
- 选择：打开天窗 → episode-004
- 选择：留在原地 → episode-005
"""
        )
        endings = [
            (2, "东门", "正式结局", "从东门离开"),
            (3, "西门", "正式结局", "从西门离开"),
            (4, "天窗", "正式结局", "从天窗离开"),
            (5, "原地", "失败小结局", "留在原地被困"),
        ]
        for number, title, interaction, fact in endings:
            nodes.append(
                f"""## episode-{number:03d}｜{title}
- 互动类型：{interaction}
- 结局：是
- 后续节点：无
"""
            )
        directory = Path(tempfile.mkdtemp())
        topology = directory / "topology.md"
        topology.write_text("\n".join(nodes), encoding="utf-8")
        self.assertEqual(module.validate_emotional_topology(topology, None), [])

    def test_dialogue_quote_is_rejected(self):
        import sys

        sys.path.insert(0, str(SCRIPT_ROOT))
        try:
            module = load_module("validate_and_assemble_scripts", SCRIPT_ROOT / "validate_and_assemble_scripts.py")
        finally:
            sys.path.pop(0)
        text = """# 分集编号

episode-001

# 分集标题

开场

# 分集剧本

## 单集梗概

甲进门。

## 完整剧本

【旧屋·夜·内】

甲：“我来了。”

# 剧本分析

## 本集冲突

甲要进门。

## 前置节点编号列表

无

## 后续节点编号列表

无

# 关联角色

无

# 关联场景

无

# 关联道具

无

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
        node = {"title": "开场", "ending": True, "choices": [], "interaction": "正式结局"}
        issues = module.validate_episode("episode-001", node, text)
        self.assertTrue(any("对白必须使用人物：台词" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
