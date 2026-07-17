#!/usr/bin/env python3
"""Validate deterministic structure and file coverage for episode-generator caches."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path


NODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE_REF = re.compile(r"episode-\d{3}")
PUBLIC_HEADER = re.compile(r"^#\s+第(\d+)集", re.MULTILINE)
PLACEHOLDER = re.compile(r"(?:已完成|见公开|与.*公开.*一致|同公开|略)$")
DEFAULT_MIN_VISIBLE_CHARS = 850
DEFAULT_MAX_VISIBLE_CHARS = 1300
DEFAULT_MIN_ACTION_BEATS = 16
DEFAULT_MAX_ACTION_BEATS = 24


def read_text(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"缺少文件：{path}")
        return ""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        errors.append(f"文件为空：{path}")
    return text


def field(block: str, name: str, errors: list[str], node_id: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        errors.append(f"{node_id} 缺少字段：{name}")
        return ""
    return match.group(1)


def refs(value: str) -> set[str]:
    if value.strip() == "无":
        return set()
    return set(EPISODE_REF.findall(value))


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def episode_metrics(text: str) -> tuple[int, int]:
    visible_chars = len(re.sub(r"\s+", "", text))
    action_beats = len(re.findall(r"^\s*△", text, re.MULTILINE))
    return visible_chars, action_beats


def project_limits(manifest: str) -> tuple[int, int, int, int]:
    block = section(manifest, "单集门槛")
    chars = re.search(r"^-\s*可见字符数：\s*(\d+)\s*[—-]\s*(\d+)\s*$", block, re.MULTILINE)
    beats = re.search(r"^-\s*动作段数：\s*(\d+)\s*[—-]\s*(\d+)\s*$", block, re.MULTILINE)
    if not chars and not beats:
        return (
            DEFAULT_MIN_VISIBLE_CHARS,
            DEFAULT_MAX_VISIBLE_CHARS,
            DEFAULT_MIN_ACTION_BEATS,
            DEFAULT_MAX_ACTION_BEATS,
        )
    if not chars or not beats:
        raise ValueError("manifest 的单集门槛必须同时记录可见字符数和动作段数")
    limits = tuple(map(int, (*chars.groups(), *beats.groups())))
    if limits[0] > limits[1] or limits[2] > limits[3]:
        raise ValueError("manifest 的单集门槛上下界无效")
    return limits


def parse_topology(text: str, errors: list[str]) -> dict[str, dict[str, object]]:
    matches = list(NODE_HEADER.finditer(text))
    if not matches:
        errors.append("topology.md 未找到固定格式节点")
        return {}

    nodes: dict[str, dict[str, object]] = {}
    for index, match in enumerate(matches):
        node_id = match.group(1)
        if node_id in nodes:
            errors.append(f"节点编号重复：{node_id}")
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        predecessors = refs(field(block, "前置节点", errors, node_id))
        successors = refs(field(block, "后续节点", errors, node_id))
        interaction = field(block, "互动类型", errors, node_id)
        ending = field(block, "结局", errors, node_id)
        choice_targets = set(
            re.findall(r"^-\s*选择：.*?(?:->|→)\s*(episode-\d{3})\s*$", block, re.MULTILINE)
        )
        nodes[node_id] = {
            "title": match.group(2).strip(),
            "predecessors": predecessors,
            "successors": successors,
            "interaction": interaction,
            "ending": ending,
            "choice_targets": choice_targets,
        }
    return nodes


def validate_graph(nodes: dict[str, dict[str, object]], errors: list[str]) -> None:
    if not nodes:
        return

    node_ids = set(nodes)
    incoming: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        successors = node["successors"]
        assert isinstance(successors, set)
        unknown = successors - node_ids
        if unknown:
            errors.append(f"{node_id} 指向不存在节点：{', '.join(sorted(unknown))}")
        for target in successors & node_ids:
            incoming[target].add(node_id)

        ending = node["ending"]
        if ending not in {"是", "否"}:
            errors.append(f"{node_id} 的结局字段必须为“是”或“否”")
        elif ending == "是" and successors:
            errors.append(f"结局节点仍有出边：{node_id}")
        elif ending == "否" and not successors:
            errors.append(f"非结局节点没有出边：{node_id}")

        choice_targets = node["choice_targets"]
        assert isinstance(choice_targets, set)
        missing_edges = choice_targets - successors
        if missing_edges:
            errors.append(f"{node_id} 的选项目标未列入后续节点：{', '.join(sorted(missing_edges))}")
        interaction = node["interaction"]
        assert isinstance(interaction, str)
        if "选择" in interaction:
            if not choice_targets:
                errors.append(f"选择节点缺少选项：{node_id}")
            elif choice_targets != successors:
                errors.append(f"{node_id} 的选项目标与后续节点不一致")
        elif choice_targets:
            errors.append(f"非选择节点包含选项：{node_id}")
        elif ending == "否" and len(successors) != 1:
            errors.append(f"非选择、非结局节点必须只有一个后续：{node_id}")

    entries = sorted(node_id for node_id in node_ids if not incoming[node_id])
    if entries != ["episode-001"]:
        errors.append(f"唯一入口必须为 episode-001，当前入口：{', '.join(entries) or '无'}")

    for node_id, node in nodes.items():
        declared = node["predecessors"]
        assert isinstance(declared, set)
        actual = incoming[node_id]
        if declared != actual:
            errors.append(
                f"{node_id} 前置节点不一致：声明 {sorted(declared)}，实际 {sorted(actual)}"
            )

    reached: set[str] = set()
    queue: deque[str] = deque(["episode-001"] if "episode-001" in nodes else [])
    while queue:
        node_id = queue.popleft()
        if node_id in reached:
            continue
        reached.add(node_id)
        successors = nodes[node_id]["successors"]
        assert isinstance(successors, set)
        queue.extend(sorted(successors & node_ids))
    unreachable = node_ids - reached
    if unreachable:
        errors.append(f"存在不可达节点：{', '.join(sorted(unreachable))}")

    indegree = {node_id: len(incoming[node_id]) for node_id in node_ids}
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        successors = nodes[node_id]["successors"]
        assert isinstance(successors, set)
        for target in successors & node_ids:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(node_ids):
        errors.append("拓扑中存在循环")


def validate_branch_audit(
    text: str,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
) -> None:
    for heading in ("目标轴", "候选岔点", "前五分钟交互", "审计结论"):
        if not section(text, heading):
            errors.append(f"branch-audit.md 缺少有效栏目：{heading}")

    fork_pattern = re.compile(
        r"^###\s+(fork-\d{3})\s*｜\s*(.+?)\s*$\n(.*?)(?=^###\s+|^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    forks = list(fork_pattern.finditer(text))
    if not forks:
        errors.append("branch-audit.md 未记录候选岔点")
    for match in forks:
        fork_id = match.group(1)
        block = match.group(3)
        handling = re.search(r"^-\s*处理：\s*(保留|删除)\s*$", block, re.MULTILINE)
        landing = re.search(
            r"^-\s*落图节点：\s*(episode-\d{3}|无)\s*$", block, re.MULTILINE
        )
        if not handling:
            errors.append(f"{fork_id} 缺少有效处理结论")
            continue
        if not landing:
            errors.append(f"{fork_id} 缺少有效落图节点")
            continue
        decision = handling.group(1)
        node_id = landing.group(1)
        if decision == "保留":
            if node_id == "无" or node_id not in nodes:
                errors.append(f"{fork_id} 保留但未指向有效拓扑节点")
            elif "选择" not in str(nodes[node_id]["interaction"]):
                errors.append(f"{fork_id} 指向的 {node_id} 不是选择节点")
        elif node_id != "无":
            errors.append(f"{fork_id} 已删除但仍声明落图节点 {node_id}")
        if decision == "删除" and not re.search(r"^-\s*理由：\s*\S+", block, re.MULTILINE):
            errors.append(f"{fork_id} 删除但未记录理由")

    early = section(text, "前五分钟交互")
    early_node = re.search(r"^-\s*落图节点：\s*(episode-\d{3})\s*$", early, re.MULTILINE)
    if not early_node:
        errors.append("branch-audit.md 未记录前五分钟交互的落图节点")
    else:
        node_id = early_node.group(1)
        if node_id not in nodes:
            errors.append(f"前五分钟交互指向不存在节点：{node_id}")
        elif "选择" not in str(nodes[node_id]["interaction"]):
            errors.append(f"前五分钟交互节点不是选择节点：{node_id}")

    if "PASS" not in section(text, "审计结论"):
        errors.append("branch-audit.md 审计结论未通过")


def validate_flowchart_svg(
    text: str,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
) -> None:
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        errors.append(f"episode-flowchart.svg 不是有效 XML：{exc}")
        return

    expected_nodes = set(nodes)
    actual_nodes = set(re.findall(r'data-node-id="(episode-\d{3})"', text))
    if actual_nodes != expected_nodes:
        errors.append(
            "静态流程图节点覆盖不一致："
            f"缺少 {sorted(expected_nodes - actual_nodes)}，"
            f"多出 {sorted(actual_nodes - expected_nodes)}"
        )

    expected_edges = {
        (node_id, target)
        for node_id, node in nodes.items()
        for target in node["successors"]
    }
    actual_edges = set(
        re.findall(
            r'data-edge-from="(episode-\d{3})"\s+data-edge-to="(episode-\d{3})"',
            text,
        )
    )
    if actual_edges != expected_edges:
        errors.append(
            "静态流程图边覆盖不一致："
            f"缺少 {sorted(expected_edges - actual_edges)}，"
            f"多出 {sorted(actual_edges - expected_edges)}"
        )


def validate_index_html(
    text: str,
    canvas_root: Path,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
) -> None:
    if '<meta name="generator" content="episode-generator">' not in text:
        errors.append("index.html 缺少 episode-generator 管理标记")
    for anchor_id, label in (
        ("game-flowchart", "游戏流程图"),
        ("full-script", "游戏完整剧本"),
    ):
        if not re.search(rf'id=["\']{re.escape(anchor_id)}["\']', text):
            errors.append(f"index.html 缺少页面区块：{anchor_id}")
        if label not in text:
            errors.append(f"index.html 缺少栏目文字：{label}")
    if "逐集剧本" not in text:
        errors.append("index.html 缺少栏目文字：逐集剧本")
    if not re.search(
        r'<img\b[^>]*src=["\']\./episode-flowchart\.svg["\'][^>]*>',
        text,
        re.IGNORECASE,
    ):
        errors.append("index.html 未直接预览 episode-flowchart.svg")
    if re.search(r'href\s*=\s*["\'][^"\']*\.md(?:[#?][^"\']*)?["\']', text, re.IGNORECASE):
        errors.append("index.html 不得以 Markdown 文件链接代替正文展示")

    expected = set(nodes)
    articles = re.findall(
        r'<article\b[^>]*\bid=["\'](episode-\d{3})["\'][^>]*'
        r'\bdata-episode-id=["\'](episode-\d{3})["\'][^>]*'
        r'\bdata-source-sha256=["\']([0-9a-f]{64})["\']',
        text,
        re.IGNORECASE,
    )
    article_ids = {article_id for article_id, data_id, _ in articles if article_id == data_id}
    if len(articles) != len(article_ids):
        errors.append("index.html 存在重复分集正文或分集 id/data-episode-id 不一致")
    if article_ids != expected:
        errors.append(
            "index.html 逐集正文覆盖不一致："
            f"缺少 {sorted(expected - article_ids)}，多出 {sorted(article_ids - expected)}"
        )

    nav_ids = set(re.findall(r'href=["\']#(episode-\d{3})["\']', text))
    jump_ids = set(re.findall(r'data-jump=["\'](episode-\d{3})["\']', text))
    if nav_ids != expected or jump_ids != expected:
        errors.append(
            "index.html 逐集页内跳转覆盖不一致："
            f"href缺少 {sorted(expected - nav_ids)}，"
            f"data-jump缺少 {sorted(expected - jump_ids)}"
        )

    source_hashes = {article_id: digest for article_id, _, digest in articles}
    for node_id in sorted(expected):
        path = canvas_root / "episodes" / f"{node_id}.md"
        if not path.is_file() or node_id not in source_hashes:
            continue
        source = path.read_text(encoding="utf-8").strip()
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if source_hashes[node_id] != actual:
            errors.append(f"index.html 展示的 {node_id} 不是当前逐集源文件版本")


def validate_episode_files(
    directory: Path,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
    public: bool,
    limits: tuple[int, int, int, int],
    stats: dict[str, tuple[int, int]],
) -> None:
    expected = set(nodes)
    actual = {path.stem for path in directory.glob("episode-*.md")} if directory.is_dir() else set()
    if expected != actual:
        errors.append(
            f"{'公开' if public else '隐藏'}分集文件不一致："
            f"缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}"
        )
    for node_id in sorted(expected & actual):
        path = directory / f"{node_id}.md"
        text = read_text(path, errors)
        number = int(node_id.rsplit("-", 1)[1])
        header = PUBLIC_HEADER.search(text)
        if not header or int(header.group(1)) != number:
            errors.append(f"分集标题编号不匹配：{path}")
        if public:
            if "单集梗概" not in text:
                errors.append(f"公开分集缺少单集梗概：{path}")
            visible_chars, action_beats = episode_metrics(text)
            stats[node_id] = (visible_chars, action_beats)
            min_chars, max_chars, min_beats, max_beats = limits
            if not min_chars <= visible_chars <= max_chars:
                errors.append(
                    f"{node_id} 可见字符数 {visible_chars} 超出 {min_chars}—{max_chars}"
                )
            if not min_beats <= action_beats <= max_beats:
                errors.append(
                    f"{node_id} 动作段数 {action_beats} 超出 {min_beats}—{max_beats}"
                )
        else:
            for heading in (
                "## 生产卡",
                "## 当前集初稿",
                "## 当前集定稿",
                "## 度量记录",
                "## 真实结尾状态",
                "## 校验状态",
            ):
                if heading not in text:
                    errors.append(f"隐藏分集缺少栏目 {heading}：{path}")
            for heading in ("当前集初稿", "当前集定稿"):
                content = section(text, heading)
                if len(re.sub(r"\s+", "", content)) < 300 or PLACEHOLDER.search(content):
                    errors.append(f"{node_id} 的{heading}不是实际完整正文")
            for heading in ("生产卡", "单集梗概"):
                content = section(text, heading)
                if not content or re.search(r"见公开|与.*公开.*一致|同公开", content):
                    errors.append(f"{node_id} 的{heading}仍含公开稿引用占位")
            metric_record = section(text, "度量记录")
            if not re.search(r"可见字符数：\s*\d+", metric_record):
                errors.append(f"{node_id} 度量记录缺少可见字符数")
            if not re.search(r"动作段数：\s*\d+", metric_record):
                errors.append(f"{node_id} 度量记录缺少动作段数")


def validate(
    canvas_root: Path,
    require_public: bool,
    limits: tuple[int, int, int, int],
) -> tuple[list[str], dict[str, tuple[int, int]]]:
    errors: list[str] = []
    stats: dict[str, tuple[int, int]] = {}
    cache = canvas_root / ".episode-cache"
    required = (
        "manifest.md",
        "global.md",
        "characters.md",
        "props.md",
        "hooks.md",
        "validation.md",
    )
    for name in required:
        read_text(cache / name, errors)

    topology = read_text(cache / "topology.md", errors)
    nodes = parse_topology(topology, errors) if topology else {}
    manifest = read_text(cache / "manifest.md", [])
    if "episode-cache-v0.1.12" in manifest and not section(manifest, "单集门槛"):
        errors.append("v0.1.12 manifest.md 缺少有效栏目：单集门槛")
    if re.search(r"episode-cache-v0\.1\.(?:8|9|10|11|12)\b", manifest):
        branch_audit = read_text(cache / "branch-audit.md", errors)
        if branch_audit:
            validate_branch_audit(branch_audit, nodes, errors)
    if re.search(r"episode-cache-v0\.1\.(?:9|10|11|12)\b", manifest):
        for heading in ("对白校验状态", "因果连续性校验状态"):
            if not section(manifest, heading):
                errors.append(f"manifest.md 缺少有效栏目：{heading}")
        if "## 对白校验线程" in manifest or "## 因果连续性校验线程" in manifest:
            errors.append("v0.1.9 及以上 manifest 不得保存校验线程或 threadId")
    count_match = re.search(r"^## 节点总数\s*\n+\s*(\d+)\s*$", manifest, re.MULTILINE)
    if not count_match:
        errors.append("manifest.md 缺少有效的节点总数")
    elif int(count_match.group(1)) != len(nodes):
        errors.append(
            f"manifest 节点总数为 {count_match.group(1)}，topology 实际为 {len(nodes)}"
        )
    validate_graph(nodes, errors)
    validate_episode_files(cache / "episodes", nodes, errors, public=False, limits=limits, stats=stats)

    if require_public:
        structure = read_text(canvas_root / "episode-structure.md", errors)
        if re.search(r"episode-cache-v0\.1\.(?:10|11|12)\b", manifest):
            if "episode-flowchart.svg" not in structure:
                errors.append("episode-structure.md 未引用静态流程图")
            if "<details>" not in structure or "```mermaid" not in structure:
                errors.append("episode-structure.md 未折叠保留 Mermaid 可编辑源码")
            flowchart = read_text(canvas_root / "episode-flowchart.svg", errors)
            if flowchart:
                validate_flowchart_svg(flowchart, nodes, errors)
        if re.search(r"episode-cache-v0\.1\.(?:11|12)\b", manifest):
            index_html = read_text(canvas_root / "index.html", errors)
            if index_html:
                validate_index_html(index_html, canvas_root, nodes, errors)
        script = read_text(canvas_root / "episode-script.md", errors)
        expected_numbers = {int(node_id.rsplit("-", 1)[1]) for node_id in nodes}
        actual_numbers = {int(number) for number in PUBLIC_HEADER.findall(script)}
        if expected_numbers != actual_numbers:
            errors.append(
                "完整剧本汇总覆盖不一致："
                f"缺少 {sorted(expected_numbers - actual_numbers)}，"
                f"多出 {sorted(actual_numbers - expected_numbers)}"
            )
        validate_episode_files(canvas_root / "episodes", nodes, errors, public=True, limits=limits, stats=stats)
        for node_id in sorted(nodes):
            public_episode = canvas_root / "episodes" / f"{node_id}.md"
            if public_episode.is_file():
                episode_text = public_episode.read_text(encoding="utf-8").strip()
                if episode_text and episode_text not in script:
                    errors.append(f"完整剧本汇总未原样包含：{public_episode}")
                if "episode-cache-v0.1.12" in manifest:
                    cache_episode = cache / "episodes" / f"{node_id}.md"
                    cache_text = read_text(cache_episode, errors)
                    final_text = section(cache_text, "当前集定稿")
                    if final_text != episode_text:
                        errors.append(f"{node_id} 的隐藏定稿与公开逐集文件不一致")
                    metric_record = section(cache_text, "度量记录")
                    visible_chars, action_beats = episode_metrics(episode_text)
                    recorded_chars = re.search(r"可见字符数：\s*(\d+)", metric_record)
                    recorded_beats = re.search(r"动作段数：\s*(\d+)", metric_record)
                    if not recorded_chars or int(recorded_chars.group(1)) != visible_chars:
                        errors.append(f"{node_id} 的缓存可见字符数与公开稿不一致")
                    if not recorded_beats or int(recorded_beats.group(1)) != action_beats:
                        errors.append(f"{node_id} 的缓存动作段数与公开稿不一致")
                    if "- 判定：PASS" not in metric_record:
                        errors.append(f"{node_id} 的机械门禁未记录为 PASS")
                    status = section(cache_text, "校验状态")
                    for name, value in (
                        ("机械完整度", "PASS"),
                        ("中文对白", "PASS"),
                        ("因果连续性", "PASS"),
                        ("冻结状态", "已冻结"),
                    ):
                        if not re.search(rf"^-\s*{name}：\s*{value}\s*$", status, re.MULTILINE):
                            errors.append(f"{node_id} 的{name}未记录为{value}")

    return errors, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--require-public", action="store_true")
    parser.add_argument("--report-stats", action="store_true")
    parser.add_argument("--min-visible-chars", type=int)
    parser.add_argument("--max-visible-chars", type=int)
    parser.add_argument("--min-action-beats", type=int)
    parser.add_argument("--max-action-beats", type=int)
    args = parser.parse_args()

    try:
        manifest_path = args.canvas_root.resolve() / ".episode-cache" / "manifest.md"
        limits = project_limits(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    overrides = (
        args.min_visible_chars,
        args.max_visible_chars,
        args.min_action_beats,
        args.max_action_beats,
    )
    limits = tuple(value if value is not None else limits[index] for index, value in enumerate(overrides))
    errors, stats = validate(args.canvas_root.resolve(), args.require_public, limits)
    if args.report_stats and stats:
        print("episode\tvisible_chars\taction_beats\tstatus")
        for node_id, (visible_chars, action_beats) in sorted(stats.items()):
            passed = (
                limits[0] <= visible_chars <= limits[1]
                and limits[2] <= action_beats <= limits[3]
            )
            print(f"{node_id}\t{visible_chars}\t{action_beats}\t{'PASS' if passed else 'FAIL'}")
    if errors:
        for index, message in enumerate(errors, 1):
            print(f"[{index}] {message}", file=sys.stderr)
        return 1
    print("episode cache validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
