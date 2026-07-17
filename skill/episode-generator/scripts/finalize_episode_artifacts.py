#!/usr/bin/env python3
"""Record drafts and freeze only episodes with current, evidenced semantic audits."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from cache_paths import cache_root_for
from validate_storyboard_fields import build_storyboard_fields, validate_fields


CAUSAL_FIELDS = (
    "上游事实",
    "地点与权限",
    "事件与反应链",
    "物件与状态",
    "后续进入条件",
)
DIALOGUE_FIELDS = (
    "话茬与当下目的",
    "人物声音",
    "设定发布与承重句",
    "朴素中文",
)


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def replace_section(text: str, heading: str, content: str) -> str:
    pattern = re.compile(
        rf"(^##\s+{re.escape(heading)}\s*$\n)(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    replacement = rf"\g<1>{content.strip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    anchor = re.search(r"^##\s+真实结尾状态\s*$", text, re.MULTILINE)
    block = f"## {heading}\n{content.strip()}\n\n"
    if anchor:
        return text[: anchor.start()] + block + text[anchor.start() :]
    return text.rstrip() + "\n\n" + block


def metrics(text: str) -> tuple[int, int]:
    return len(re.sub(r"\s+", "", text)), len(re.findall(r"^\s*△", text, re.MULTILINE))


def digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def pending_audit(fields: tuple[str, ...]) -> str:
    lines = ["- 状态：PENDING", "- 正文 SHA-256：PENDING"]
    lines.extend(f"- {name}：PENDING｜待当前主 Agent 复核" for name in fields)
    lines.append("- 未解决问题：待复核")
    return "\n".join(lines)


def validate_audit(
    cache_text: str,
    heading: str,
    required_fields: tuple[str, ...],
    expected_digest: str,
    episode_id: str,
) -> None:
    block = section(cache_text, heading)
    if not block:
        raise SystemExit(f"{episode_id} 缺少{heading}")
    if field(block, "状态") != "PASS":
        raise SystemExit(f"{episode_id} 的{heading}未 PASS")
    if field(block, "正文 SHA-256") != expected_digest:
        raise SystemExit(f"{episode_id} 的{heading}正文指纹已失效，必须重跑复核")
    for name in required_fields:
        value = field(block, name)
        match = re.fullmatch(r"PASS[｜|]\s*(\S.*)", value)
        if not match or len(re.sub(r"\s+", "", match.group(1))) < 8:
            raise SystemExit(f"{episode_id} 的{heading}缺少具体依据：{name}")
    if field(block, "未解决问题") != "无":
        raise SystemExit(f"{episode_id} 的{heading}仍有未解决问题")


def selected_paths(root: Path, selection: str | None) -> list[Path]:
    cache_dir = cache_root_for(root) / "episodes"
    all_paths = sorted(cache_dir.glob("episode-*.md"))
    if not all_paths:
        raise SystemExit("未找到 Canvas 外私有分集缓存")
    if not selection:
        return all_paths
    requested = {item.strip() for item in selection.split(",") if item.strip()}
    known = {path.stem: path for path in all_paths}
    missing = sorted(requested - set(known))
    if missing:
        raise SystemExit(f"未找到指定分集：{', '.join(missing)}")
    return [known[name] for name in sorted(requested)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--mode", choices=("record-draft", "finalize"), required=True)
    parser.add_argument("--episodes", help="逗号分隔的 episode-NNN；省略时处理全部")
    parser.add_argument("--min-visible-chars", type=int, default=850)
    parser.add_argument("--max-visible-chars", type=int, default=1300)
    parser.add_argument("--min-action-beats", type=int, default=16)
    parser.add_argument("--max-action-beats", type=int, default=24)
    args = parser.parse_args()

    root = args.canvas_root.resolve()
    cache_root = cache_root_for(root)
    topology = (cache_root / "topology.md").read_text(encoding="utf-8")
    targets = selected_paths(root, args.episodes)

    for cache_path in targets:
        episode_id = cache_path.stem
        cache_text = cache_path.read_text(encoding="utf-8")

        if args.mode == "record-draft":
            draft_text = section(cache_text, "当前集初稿")
            if len(re.sub(r"\s+", "", draft_text)) < 300:
                raise SystemExit(f"{episode_id} 当前集初稿不是实际完整正文")
            cache_text = replace_section(cache_text, "当前集定稿", "PENDING")
            cache_text = replace_section(cache_text, "度量记录", "- 判定：PENDING")
            cache_text = replace_section(
                cache_text, "因果复核记录", pending_audit(CAUSAL_FIELDS)
            )
            cache_text = replace_section(
                cache_text, "对白复核记录", pending_audit(DIALOGUE_FIELDS)
            )
            cache_text = replace_section(
                cache_text,
                "校验状态",
                "- 机械完整度：PENDING\n- 因果连续性：PENDING\n- 中文对白：PENDING\n- 冻结状态：未冻结",
            )
        else:
            candidate_text = section(cache_text, "当前集定稿").strip()
            if len(re.sub(r"\s+", "", candidate_text)) < 300:
                raise SystemExit(f"{episode_id} 当前集定稿不是实际完整正文")
            synopsis_match = re.search(r"^单集梗概：\s*(.+)$", candidate_text, re.MULTILINE)
            if not synopsis_match:
                raise SystemExit(f"{episode_id} 当前集定稿缺少单集梗概")
            cache_text = replace_section(cache_text, "单集梗概", synopsis_match.group(1))
            visible_chars, action_beats = metrics(candidate_text)
            if not args.min_visible_chars <= visible_chars <= args.max_visible_chars:
                raise SystemExit(
                    f"{episode_id} 可见字符数 {visible_chars} 超出 "
                    f"{args.min_visible_chars}—{args.max_visible_chars}"
                )
            if not args.min_action_beats <= action_beats <= args.max_action_beats:
                raise SystemExit(
                    f"{episode_id} 动作段数 {action_beats} 超出 "
                    f"{args.min_action_beats}—{args.max_action_beats}"
                )
            current_digest = digest(candidate_text)
            validate_audit(
                cache_text, "因果复核记录", CAUSAL_FIELDS, current_digest, episode_id
            )
            validate_audit(
                cache_text, "对白复核记录", DIALOGUE_FIELDS, current_digest, episode_id
            )
            record = (
                f"- 可见字符数：{visible_chars}\n"
                f"- 动作段数：{action_beats}\n"
                f"- 门槛：{args.min_visible_chars}—{args.max_visible_chars} 个去空白可见字符；"
                f"{args.min_action_beats}—{args.max_action_beats} 个动作段\n"
                "- 判定：PASS"
            )
            cache_text = replace_section(cache_text, "度量记录", record)
            cache_text = replace_section(
                cache_text,
                "校验状态",
                "- 机械完整度：PASS\n"
                "- 因果连续性：PASS（见当前正文指纹对应的因果复核记录）\n"
                "- 中文对白：PASS（见当前正文指纹对应的对白复核记录）\n"
                "- 冻结状态：已冻结",
            )
            public_path = root / "episodes" / cache_path.name
            public_path.parent.mkdir(parents=True, exist_ok=True)
            public_path.write_text(candidate_text + "\n", encoding="utf-8")
        cache_path.write_text(cache_text.rstrip() + "\n", encoding="utf-8")
        if args.mode == "finalize":
            validate_fields(build_storyboard_fields(cache_text, topology))

    if args.mode == "finalize":
        all_public = sorted((root / "episodes").glob("episode-*.md"))
        combined = "\n\n---\n\n".join(
            path.read_text(encoding="utf-8").strip() for path in all_public
        )
        (root / "episode-script.md").write_text(combined + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
