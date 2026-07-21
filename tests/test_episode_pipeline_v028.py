from __future__ import annotations

import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skill" / "episode-generator" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from load_reference_bundle import load_bundle, write_receipt  # noqa: E402
from seal_pipeline_stage import resume_status, seal_stage, verify_stage  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def reference_receipts(cache: Path) -> None:
    phases = (
        "upstream",
        "emotional-spine",
        "causal-graph",
        "topology",
        "production-cards",
        "episode-writing",
        "scene-review",
        "dialogue-review",
        "isolated-audience",
        "state-writeback",
        "whole-play-review",
    )
    for phase in phases:
        write_receipt(cache / ".reference-receipts", load_bundle(phase))


def emotional_spine() -> str:
    rows = (
        ("ES-001", "开场", "开场钩子", "(-0.8,0.2,-0.8)", "(0.6,0.8,0.5)"),
        ("ES-002", "释放", "首次释放", "(-0.5,0.1,-0.7)", "(0.7,0.7,0.6)"),
        ("ES-003", "反噬", "中段反噬", "(0.7,0.6,0.5)", "(-0.8,0.9,-0.8)"),
        ("ES-004", "高潮", "高潮解法", "(-0.8,0.8,-0.8)", "(0.9,0.9,0.8)"),
    )
    blocks = []
    import math
    import re

    for event_id, title, function, start, end in rows:
        start_values = tuple(map(float, re.findall(r"-?\d\.\d", start)))
        end_values = tuple(map(float, re.findall(r"-?\d\.\d", end)))
        distance = math.dist(start_values, end_values)
        blocks.append(
            f"""## {event_id}｜{title}
- 阶段功能：{function}
- 当前欲望：主角要夺回眼前具体机会
- 当前恐惧：主角害怕失去伙伴和决定权
- 压力来源：已经发生的损失逼迫主角立即选择
- 造成行动：主角当场采取会改变关系的行动
- 不可逆结果：行动留下无法撤销的承诺和损失
- 下一催化：受损一方主动采取下一步反制行动
- 起点VAD：{start}
- 落点VAD：{end}
- 情绪位移：{distance:.2f}
- 强度处理：原始通过"""
        )
    return "# 情绪脊\n\n" + "\n\n".join(blocks)


def causal_graph() -> str:
    return """# 因果图

## CG-001｜机会出现
- 情绪事件：ES-001
- 原因：上游异常让主角看见尚未公开的机会
- 人物行动：主角先占住关键资源并寻找买家
- 阻力：伙伴要求先核实来源而买家催促成交
- 不可逆结果：主角第一次用异常完成交易并留下记录
- 下一事件：CG-002, CG-003

## CG-002｜共同留证
- 情绪事件：ES-002
- 原因：第一次交易的记录出现时间倒置
- 人物行动：主角把原始证据交给伙伴分别保管
- 阻力：公开证据会削弱主角独占机会的优势
- 不可逆结果：伙伴取得共同停机权并改变公司治理
- 下一事件：CG-004

## CG-003｜秘密独占
- 情绪事件：ES-003
- 原因：第一次交易的记录出现时间倒置
- 人物行动：主角删除原始证据并否认异常存在
- 阻力：伙伴已经亲眼看见记录变化并拒绝配合
- 不可逆结果：伙伴退出设备管理并保留外部证据
- 下一事件：CG-004

## CG-004｜最终处置
- 情绪事件：ES-004
- 原因：两条路线留下的治理差异在危机中同时兑现
- 人物行动：主角使用已有证据和权限处置异常节点
- 阻力：关闭能力会失去财富而保留能力会损伤记忆
- 不可逆结果：主角选定财富或关系并永久失去另一项
- 下一事件：结局
"""


def topology() -> str:
    return """# 分集拓扑

## episode-001｜第一次交易
- 前置节点：无
- 后续节点：episode-002, episode-003
- 互动类型：决策性选择
- 情绪事件：ES-001
- 因果事件：CG-001
- 选择：交出原始记录 -> episode-002
- 选择：删除原始记录 -> episode-003
- 选择后果：episode-002｜ES-002｜CG-002｜伙伴取得共同停机权并改变后续治理
- 选择后果：episode-003｜ES-003｜CG-003｜伙伴退出设备管理并带走外部证据
- 结局：否

## episode-002｜共同处置
- 前置节点：episode-001
- 后续节点：无
- 互动类型：正式结局
- 情绪事件：ES-002, ES-004
- 因果事件：CG-002, CG-004
- 结局：是

## episode-003｜独占代价
- 前置节点：episode-001
- 后续节点：无
- 互动类型：正式结局
- 情绪事件：ES-003, ES-004
- 因果事件：CG-003, CG-004
- 结局：是
"""


def episode_file(episode_id: str, emotion: str, causal: str) -> str:
    return f"""# 分集

## 生产卡
- 分集容器：{episode_id}
- 情绪事件：{emotion}
- 因果事件：{causal}
- 核心事件：人物执行拓扑冻结的具体行动并承担结果

## 单集梗概
人物执行一项不可逆行动，并把后果带入下一节点或结局。

## 本集冲突与前后承接
承接前置真实结果，在眼前目标与伙伴关系之间完成一次选择。

## 前置节点
见冻结拓扑中的直接前置节点。

## 后续节点
见冻结拓扑中的直接后续节点。

## 题材透镜记录
- 具体事件：人物使用联网设备完成一笔可核验交易
- 题材反差物：未来记录先于真实请求出现在旧电脑
- 题材内升级：交易越成功现实记录越早发生错位
- 后续催化剂或结局余波：伙伴据此夺取权限或退出公司
- 判定：PASS

## 当前集初稿
# 第1集《测试》
单集梗概：人物执行行动。
【场一 · 办公室 · 日 · 内】
△ 人物把记录放到桌上。
人物：这次按已经说好的办。

## 当前集定稿
# 第1集《测试》
单集梗概：人物执行行动。
【场一 · 办公室 · 日 · 内】
△ 人物把记录放到桌上。
人物：这次按已经说好的办。

## 场面事实复核记录
- 状态：PASS

## 人物交流复核记录
- 状态：PASS

## 隔离观众复核记录
- 状态：PASS

## 校验状态
- 冻结状态：已冻结

## 真实结尾状态
人物完成不可逆行动，关系与权限已经改变。
"""


def build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    cache = tmp_path / "cache"
    canvas = tmp_path / "canvas"
    canvas.mkdir()
    reference_receipts(cache)
    write(cache / "input.md", "# 输入\n实际输入内容足够长且保持稳定")
    write(cache / "upstream-packet.md", "# 阅读包\n确定性的上游阅读内容")
    write(cache / "upstream-reference.md", "# 上游参考\n冻结后的制作事实与规则")
    write(cache / "emotional-spine.md", emotional_spine())
    write(cache / "causal-graph.md", causal_graph())
    write(cache / "topology.md", topology())
    write(cache / "branch-audit.md", "# 分支审计\n冻结候选与采用依据")
    write(cache / "episodes/episode-001.md", episode_file("episode-001", "ES-001", "CG-001"))
    write(cache / "episodes/episode-002.md", episode_file("episode-002", "ES-002, ES-004", "CG-002, CG-004"))
    write(cache / "episodes/episode-003.md", episode_file("episode-003", "ES-003, ES-004", "CG-003, CG-004"))
    for name in ("global.md", "characters.md", "props.md", "hooks.md", "validation.md"):
        write(cache / name, f"# {name}\n冻结后的具体状态内容")
    write(canvas / "episode-synopsis.md", "# 各集梗概\n完整公开梗概")
    write(canvas / "episode-flowchart.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>流程</text></svg>')
    write(canvas / "episode-script.md", "# 完整剧本\n完整公开正文")
    return cache, canvas


class EpisodePipelineV028Test(unittest.TestCase):
    def test_full_chain_and_upstream_mutation_invalidate_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache, canvas = build_fixture(Path(directory))
            for stage in (
                "upstream",
                "emotional-spine",
                "causal-graph",
                "topology",
                "production-cards",
                "drafts",
                "reviews",
                "state-writeback",
                "final",
            ):
                seal_stage(cache, canvas, stage)
            verify_stage(cache, canvas, "final")

            write(
                cache / "emotional-spine.md",
                emotional_spine().replace("主角要夺回", "主角急于夺回", 1),
            )
            with self.assertRaisesRegex(ValueError, "过期|无法追溯"):
                verify_stage(cache, canvas, "final")

    def test_stage_cannot_skip_previous_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache, canvas = build_fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, "缺少阶段封印"):
                seal_stage(cache, canvas, "causal-graph")

    def test_resume_status_keeps_completed_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache, canvas = build_fixture(Path(directory))
            seal_stage(cache, canvas, "upstream")
            seal_stage(cache, canvas, "emotional-spine")
            status = resume_status(cache, canvas)
            self.assertEqual(status["last_valid_stage"], "emotional-spine")
            self.assertEqual(status["resume_from_stage"], "causal-graph")
            self.assertEqual(status["reason"], "missing")

    def test_public_flowchart_is_blocked_before_state_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache, canvas = build_fixture(Path(directory))
            write(cache / "manifest.md", "# 项目\nepisode-cache-v0.1.28")
            for stage in ("upstream", "emotional-spine", "causal-graph", "topology"):
                seal_stage(cache, canvas, stage)
            output = canvas / "blocked.svg"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_ROOT / "render_episode_flowchart.py"),
                    str(canvas),
                    "--cache-root",
                    str(cache),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
