#!/usr/bin/env python3
"""Render the frozen episode topology as a self-contained static SVG."""

from __future__ import annotations

import argparse
import html
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from cache_paths import cache_root_for


NODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE_REF = re.compile(r"episode-\d{3}")
NODE_WIDTH = 250
NODE_HEIGHT = 74
HORIZONTAL_GAP = 105
VERTICAL_GAP = 120
MARGIN_X = 90
MARGIN_Y = 92


@dataclass(frozen=True)
class Node:
    node_id: str
    title: str
    successors: tuple[str, ...]
    choices: dict[str, str]
    interaction: str
    ending: bool


def field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        raise ValueError(f"缺少字段：{name}")
    return match.group(1).strip()


def parse_topology(text: str) -> dict[str, Node]:
    matches = list(NODE_HEADER.finditer(text))
    if not matches:
        raise ValueError("topology.md 未找到固定格式节点")
    nodes: dict[str, Node] = {}
    for index, match in enumerate(matches):
        node_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        successor_text = field(block, "后续节点")
        successors = tuple(EPISODE_REF.findall(successor_text)) if successor_text != "无" else ()
        choices = {
            target: label.strip()
            for label, target in re.findall(
                r"^-\s*选择：\s*(.*?)\s*(?:->|→)\s*(episode-\d{3})\s*$",
                block,
                re.MULTILINE,
            )
        }
        nodes[node_id] = Node(
            node_id=node_id,
            title=match.group(2).strip(),
            successors=successors,
            choices=choices,
            interaction=field(block, "互动类型"),
            ending=field(block, "结局") == "是",
        )
    return nodes


def topological_layers(nodes: dict[str, Node]) -> tuple[list[str], dict[str, int]]:
    incoming: dict[str, set[str]] = defaultdict(set)
    indegree = {node_id: 0 for node_id in nodes}
    for node in nodes.values():
        for target in node.successors:
            if target not in nodes:
                raise ValueError(f"{node.node_id} 指向不存在节点：{target}")
            if node.node_id not in incoming[target]:
                incoming[target].add(node.node_id)
                indegree[target] += 1

    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    order: list[str] = []
    rank = {node_id: 0 for node_id in nodes}
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for target in nodes[node_id].successors:
            rank[target] = max(rank[target], rank[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(nodes):
        raise ValueError("拓扑中存在循环")
    return order, rank


def wrapped_lines(text: str, limit: int) -> list[str]:
    compact = text.strip()
    if len(compact) <= limit:
        return [compact]
    return [compact[:limit], compact[limit : limit * 2 - 1] + ("…" if len(compact) > limit * 2 - 1 else "")]


def render_svg(nodes: dict[str, Node], title: str) -> str:
    order, rank = topological_layers(nodes)
    layers: dict[int, list[str]] = defaultdict(list)
    for node_id in order:
        layers[rank[node_id]].append(node_id)
    max_layer_size = max(len(layer) for layer in layers.values())
    width = max(
        720,
        MARGIN_X * 2 + max_layer_size * NODE_WIDTH + (max_layer_size - 1) * HORIZONTAL_GAP,
    )
    max_rank = max(layers)
    height = MARGIN_Y * 2 + (max_rank + 1) * NODE_HEIGHT + max_rank * VERTICAL_GAP

    positions: dict[str, tuple[float, float]] = {}
    for layer_rank in range(max_rank + 1):
        layer = layers[layer_rank]
        layer_width = len(layer) * NODE_WIDTH + max(0, len(layer) - 1) * HORIZONTAL_GAP
        start_x = (width - layer_width) / 2
        y = MARGIN_Y + layer_rank * (NODE_HEIGHT + VERTICAL_GAP)
        for index, node_id in enumerate(layer):
            positions[node_id] = (start_x + index * (NODE_WIDTH + HORIZONTAL_GAP), y)

    escaped_title = html.escape(title)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="flow-title flow-desc">',
        f'<title id="flow-title">{escaped_title}</title>',
        '<desc id="flow-desc">互动影视分集节点与选择流程图</desc>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#796455"/></marker></defs>',
        '<rect width="100%" height="100%" rx="24" fill="#fbf7ef"/>',
        f'<text x="{width / 2}" y="42" text-anchor="middle" font-family="PingFang SC, Microsoft YaHei, sans-serif" font-size="24" font-weight="700" fill="#2c211b">{escaped_title}</text>',
    ]

    for node_id in order:
        source = nodes[node_id]
        sx, sy = positions[node_id]
        start_x = sx + NODE_WIDTH / 2
        start_y = sy + NODE_HEIGHT
        for target in source.successors:
            tx, ty = positions[target]
            end_x = tx + NODE_WIDTH / 2
            end_y = ty
            bend = max(38, (end_y - start_y) * 0.48)
            path = f"M {start_x:.1f} {start_y:.1f} C {start_x:.1f} {start_y + bend:.1f}, {end_x:.1f} {end_y - bend:.1f}, {end_x:.1f} {end_y:.1f}"
            parts.append(
                f'<path data-edge-from="{node_id}" data-edge-to="{target}" d="{path}" fill="none" stroke="#796455" stroke-width="2.2" marker-end="url(#arrow)"/>'
            )
            label = source.choices.get(target, "")
            if label:
                label_lines = wrapped_lines(label, 13)
                label_x = (start_x + end_x) / 2
                label_y = (start_y + end_y) / 2 - 5
                box_width = min(235, max(96, max(len(line) for line in label_lines) * 15 + 22))
                box_height = 24 + (len(label_lines) - 1) * 17
                parts.append(
                    f'<rect x="{label_x - box_width / 2:.1f}" y="{label_y - 17:.1f}" width="{box_width}" height="{box_height}" rx="9" fill="#fbf7ef" stroke="#d7c7b8"/>'
                )
                for line_index, line in enumerate(label_lines):
                    parts.append(
                        f'<text x="{label_x:.1f}" y="{label_y + line_index * 17:.1f}" text-anchor="middle" font-family="PingFang SC, Microsoft YaHei, sans-serif" font-size="13" fill="#5c493d">{html.escape(line)}</text>'
                    )

    for node_id in order:
        node = nodes[node_id]
        x, y = positions[node_id]
        if node.ending:
            fill, stroke, stroke_width, radius = "#f8e1dc", "#b75845", 3, 18
        elif "选择" in node.interaction:
            fill, stroke, stroke_width, radius = "#fff0cf", "#a46f22", 3, 28
        else:
            fill, stroke, stroke_width, radius = "#f2eadf", "#796455", 2.4, 18
        parts.append(f'<g data-node-id="{node_id}">')
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_WIDTH}" height="{NODE_HEIGHT}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )
        short_id = node_id.rsplit("-", 1)[1]
        parts.append(
            f'<text x="{x + 20:.1f}" y="{y + 27:.1f}" font-family="SFMono-Regular, Menlo, monospace" font-size="14" font-weight="700" fill="{stroke}">{short_id}</text>'
        )
        title_lines = wrapped_lines(node.title, 14)
        title_x = x + NODE_WIDTH / 2 + 13
        first_y = y + 29 - (len(title_lines) - 1) * 8
        for line_index, line in enumerate(title_lines):
            parts.append(
                f'<text x="{title_x:.1f}" y="{first_y + line_index * 20:.1f}" text-anchor="middle" font-family="PingFang SC, Microsoft YaHei, sans-serif" font-size="16" font-weight="650" fill="#2c211b">{html.escape(line)}</text>'
            )
        parts.append("</g>")

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--title")
    args = parser.parse_args()

    root = args.canvas_root.resolve()
    topology_path = cache_root_for(root) / "topology.md"
    nodes = parse_topology(topology_path.read_text(encoding="utf-8"))
    title = args.title or f"{root.name} 分集流程图"
    output = args.output.resolve() if args.output else root / "episode-flowchart.svg"
    output.write_text(render_svg(nodes, title), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
