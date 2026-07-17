#!/usr/bin/env python3
"""Gate hidden episode drafts, project frozen finals, and assemble the public script."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


EPISODE_ID = re.compile(r"episode-\d{3}")
DEFAULT_LIMITS = (850, 1300, 16, 24)


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
    if not pattern.search(text):
        raise ValueError(f"隐藏分集缺少栏目：{heading}")
    return pattern.sub(rf"\g<1>{content.strip()}\n\n", text, count=1)


def metrics(text: str) -> tuple[int, int]:
    return len(re.sub(r"\s+", "", text)), len(re.findall(r"^\s*△", text, re.MULTILINE))


def project_limits(manifest: str) -> tuple[int, int, int, int]:
    block = section(manifest, "单集门槛")
    chars = re.search(r"^-\s*可见字符数：\s*(\d+)\s*[—-]\s*(\d+)\s*$", block, re.MULTILINE)
    beats = re.search(r"^-\s*动作段数：\s*(\d+)\s*[—-]\s*(\d+)\s*$", block, re.MULTILINE)
    if not chars and not beats:
        return DEFAULT_LIMITS
    if not chars or not beats:
        raise ValueError("manifest 的单集门槛必须同时记录可见字符数和动作段数")
    limits = tuple(map(int, (*chars.groups(), *beats.groups())))
    if limits[0] > limits[1] or limits[2] > limits[3]:
        raise ValueError("manifest 的单集门槛上下界无效")
    return limits


def validate_script(text: str, episode_id: str) -> None:
    number = int(episode_id.rsplit("-", 1)[1])
    if not re.search(rf"^#\s+第0*{number}集《.+?》\s*$", text, re.MULTILINE):
        raise ValueError(f"{episode_id} 正文缺少匹配的分集标题")
    if not re.search(r"^单集梗概：\s*\S+", text, re.MULTILINE):
        raise ValueError(f"{episode_id} 正文缺少单集梗概")


def metric_record(text: str, limits: tuple[int, int, int, int]) -> tuple[str, bool]:
    visible_chars, action_beats = metrics(text)
    passed = limits[0] <= visible_chars <= limits[1] and limits[2] <= action_beats <= limits[3]
    record = (
        f"- 可见字符数：{visible_chars}\n"
        f"- 动作段数：{action_beats}\n"
        f"- 门槛：{limits[0]}—{limits[1]} 个去空白可见字符；{limits[2]}—{limits[3]} 个动作段\n"
        f"- 判定：{'PASS' if passed else 'FAIL'}"
    )
    return record, passed


def replace_status(text: str, name: str, value: str) -> str:
    status = section(text, "校验状态")
    pattern = re.compile(rf"^-\s*{re.escape(name)}：\s*.*$", re.MULTILINE)
    line = f"- {name}：{value}"
    status = pattern.sub(line, status, count=1) if pattern.search(status) else f"{status}\n{line}".strip()
    return replace_section(text, "校验状态", status)


def require_semantic_pass(cache_text: str, episode_id: str) -> None:
    status = section(cache_text, "校验状态")
    for name in ("中文对白", "因果连续性"):
        if not re.search(rf"^-\s*{name}：\s*PASS\s*$", status, re.MULTILINE):
            raise ValueError(f"{episode_id} 的{name}尚未由主 Agent 记录为 PASS")


def episode_cache(root: Path, episode_id: str) -> Path:
    if not EPISODE_ID.fullmatch(episode_id):
        raise ValueError("--episode 必须使用 episode-NNN 格式")
    path = root / ".episode-cache" / "episodes" / f"{episode_id}.md"
    if not path.is_file():
        raise ValueError(f"未找到隐藏分集：{path}")
    return path


def check_draft(root: Path, episode_id: str, limits: tuple[int, int, int, int]) -> None:
    cache_path = episode_cache(root, episode_id)
    cache_text = cache_path.read_text(encoding="utf-8")
    draft = section(cache_text, "当前集初稿")
    validate_script(draft, episode_id)
    record, passed = metric_record(draft, limits)
    cache_text = replace_section(cache_text, "度量记录", record)
    cache_text = replace_status(cache_text, "机械完整度", "PASS" if passed else "FAIL")
    cache_text = replace_status(cache_text, "中文对白", "待校验")
    cache_text = replace_status(cache_text, "因果连续性", "待校验")
    cache_text = replace_status(cache_text, "冻结状态", "未冻结")
    cache_path.write_text(cache_text.rstrip() + "\n", encoding="utf-8")
    if not passed:
        raise ValueError(f"{episode_id} 初稿未通过机械完整度门禁")


def finalize(root: Path, episode_id: str, limits: tuple[int, int, int, int]) -> None:
    cache_path = episode_cache(root, episode_id)
    cache_text = cache_path.read_text(encoding="utf-8")
    final_text = section(cache_text, "当前集定稿")
    validate_script(final_text, episode_id)
    require_semantic_pass(cache_text, episode_id)
    record, passed = metric_record(final_text, limits)
    cache_text = replace_section(cache_text, "度量记录", record)
    cache_text = replace_status(cache_text, "机械完整度", "PASS" if passed else "FAIL")
    cache_text = replace_status(cache_text, "冻结状态", "已冻结" if passed else "未冻结")
    cache_path.write_text(cache_text.rstrip() + "\n", encoding="utf-8")
    if not passed:
        raise ValueError(f"{episode_id} 定稿未通过机械完整度门禁，未投射公开文件")
    public_dir = root / "episodes"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / f"{episode_id}.md").write_text(final_text.rstrip() + "\n", encoding="utf-8")


def assemble(root: Path, limits: tuple[int, int, int, int]) -> None:
    topology = (root / ".episode-cache" / "topology.md").read_text(encoding="utf-8")
    expected = set(re.findall(r"^##\s+(episode-\d{3})\s*｜", topology, re.MULTILINE))
    public_files = sorted((root / "episodes").glob("episode-*.md"))
    actual = {path.stem for path in public_files}
    if actual != expected:
        raise ValueError(
            f"公开逐集覆盖与拓扑不一致：缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}"
        )
    for public_path in public_files:
        episode_id = public_path.stem
        public_text = public_path.read_text(encoding="utf-8").strip()
        cache_text = episode_cache(root, episode_id).read_text(encoding="utf-8")
        if section(cache_text, "当前集定稿") != public_text:
            raise ValueError(f"{episode_id} 的隐藏定稿与公开逐集文件不一致")
        require_semantic_pass(cache_text, episode_id)
        status = section(cache_text, "校验状态")
        if not re.search(r"^-\s*冻结状态：\s*已冻结\s*$", status, re.MULTILINE):
            raise ValueError(f"{episode_id} 尚未冻结")
        _, passed = metric_record(public_text, limits)
        if not passed:
            raise ValueError(f"{episode_id} 公开稿未通过机械完整度门禁")
    combined = "\n\n---\n\n".join(path.read_text(encoding="utf-8").strip() for path in public_files)
    (root / "episode-script.md").write_text(combined + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--mode", choices=("check-draft", "finalize", "assemble"), required=True)
    parser.add_argument("--episode")
    args = parser.parse_args()

    root = args.canvas_root.resolve()
    try:
        manifest = (root / ".episode-cache" / "manifest.md").read_text(encoding="utf-8")
        limits = project_limits(manifest)
        if args.mode in {"check-draft", "finalize"} and not args.episode:
            raise ValueError(f"--mode {args.mode} 必须指定 --episode episode-NNN")
        if args.mode == "check-draft":
            check_draft(root, args.episode, limits)
        elif args.mode == "finalize":
            finalize(root, args.episode, limits)
        else:
            assemble(root, limits)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
