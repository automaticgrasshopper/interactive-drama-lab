#!/usr/bin/env python3
"""Validate deterministic structure and file coverage for episode-generator caches."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict, deque
from pathlib import Path


NODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE_REF = re.compile(r"episode-\d{3}")
PUBLIC_HEADER = re.compile(r"^#\s+第(\d+)集", re.MULTILINE)


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


def validate_episode_files(
    directory: Path,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
    public: bool,
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
        else:
            for heading in ("## 生产卡", "## 当前集定稿", "## 真实结尾状态", "## 校验状态"):
                if heading not in text:
                    errors.append(f"隐藏分集缺少栏目 {heading}：{path}")


def validate(canvas_root: Path, require_public: bool) -> list[str]:
    errors: list[str] = []
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
    count_match = re.search(r"^## 节点总数\s*\n+\s*(\d+)\s*$", manifest, re.MULTILINE)
    if not count_match:
        errors.append("manifest.md 缺少有效的节点总数")
    elif int(count_match.group(1)) != len(nodes):
        errors.append(
            f"manifest 节点总数为 {count_match.group(1)}，topology 实际为 {len(nodes)}"
        )
    validate_graph(nodes, errors)
    validate_episode_files(cache / "episodes", nodes, errors, public=False)

    if require_public:
        read_text(canvas_root / "episode-structure.md", errors)
        script = read_text(canvas_root / "episode-script.md", errors)
        expected_numbers = {int(node_id.rsplit("-", 1)[1]) for node_id in nodes}
        actual_numbers = {int(number) for number in PUBLIC_HEADER.findall(script)}
        if expected_numbers != actual_numbers:
            errors.append(
                "完整剧本汇总覆盖不一致："
                f"缺少 {sorted(expected_numbers - actual_numbers)}，"
                f"多出 {sorted(actual_numbers - expected_numbers)}"
            )
        validate_episode_files(canvas_root / "episodes", nodes, errors, public=True)
        for node_id in sorted(nodes):
            public_episode = canvas_root / "episodes" / f"{node_id}.md"
            if public_episode.is_file():
                episode_text = public_episode.read_text(encoding="utf-8").strip()
                if episode_text and episode_text not in script:
                    errors.append(f"完整剧本汇总未原样包含：{public_episode}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--require-public", action="store_true")
    args = parser.parse_args()

    errors = validate(args.canvas_root.resolve(), args.require_public)
    if errors:
        for index, message in enumerate(errors, 1):
            print(f"[{index}] {message}", file=sys.stderr)
        return 1
    print("episode cache validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
