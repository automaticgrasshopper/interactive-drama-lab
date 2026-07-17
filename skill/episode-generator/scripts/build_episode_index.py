#!/usr/bin/env python3
"""Build a self-contained flowchart preview and episode reader from public artifacts."""

from __future__ import annotations

import argparse
import hashlib
import html
import re
from pathlib import Path


GENERATOR_MARKER = '<meta name="generator" content="episode-generator">'
EPISODE_FILE = re.compile(r"episode-(\d{3})\.md$")


def inline(text: str) -> str:
    escaped = html.escape(text.strip())
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def field_from_input(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        patterns = (
            rf"^\*\*{re.escape(name)}：\*\*\s*(.+?)\s*$",
            rf"^\*\*{re.escape(name)}\*\*\s*[:：]\s*(.+?)\s*$",
            rf"^{re.escape(name)}\s*[:：]\s*(.+?)\s*$",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                return match.group(1).strip()
    return ""


def project_title(root: Path) -> str:
    manifest = root / ".episode-cache" / "manifest.md"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        match = re.search(r"^##\s+项目\s*$\n+\s*(.+?)\s*$", text, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return root.name.replace("-", " ")


def episode_title(text: str, number: int) -> str:
    match = re.search(r"^#\s+第\d+集《(.+?)》\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else f"第{number}集"


def render_markdown_body(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            output.append('<ul class="choices">' + "".join(list_items) + "</ul>")
            list_items.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            continue
        if line.startswith("# "):
            continue
        if line.startswith("单集梗概："):
            flush_list()
            output.append(
                f'<div class="synopsis"><span>单集梗概</span><p>{inline(line.split("：", 1)[1])}</p></div>'
            )
            continue
        if line.startswith("【") and line.endswith("】"):
            flush_list()
            output.append(f'<h3 class="scene">{inline(line)}</h3>')
            continue
        if line.startswith("出场："):
            flush_list()
            output.append(f'<p class="cast">{inline(line)}</p>')
            continue
        if line.startswith("△"):
            flush_list()
            output.append(f'<p class="action">{inline(line)}</p>')
            continue
        if line in {"**请选择：**", "请选择："}:
            flush_list()
            output.append('<p class="choice-title">玩家选择</p>')
            continue
        if line.startswith("- "):
            list_items.append(f"<li>{inline(line[2:])}</li>")
            continue
        if re.fullmatch(r"\*\*结局：.+?\*\*", line):
            flush_list()
            ending = line.removeprefix("**结局：").removesuffix("**")
            output.append(f'<p class="ending">结局 · {inline(ending)}</p>')
            continue
        if line.startswith("## "):
            flush_list()
            output.append(f'<h3>{inline(line[3:])}</h3>')
            continue
        if line.startswith("### "):
            flush_list()
            output.append(f'<h4>{inline(line[4:])}</h4>')
            continue
        dialogue = re.match(r"^([^：:]{1,16})[：:]\s*(.+)$", line)
        if dialogue:
            flush_list()
            output.append(
                f'<p class="dialogue"><span class="speaker">{inline(dialogue.group(1))}</span>'
                f'<span>{inline(dialogue.group(2))}</span></p>'
            )
            continue
        flush_list()
        output.append(f"<p>{inline(line)}</p>")
    flush_list()
    return "\n".join(output)


def page(root: Path) -> str:
    title = project_title(root)
    input_path = root / ".episode-cache" / "input.md"
    input_text = input_path.read_text(encoding="utf-8") if input_path.is_file() else ""
    keywords = field_from_input(input_text, ("游戏关键词", "关键词"))
    volume = field_from_input(input_text, ("游戏体量", "体量"))
    logline = field_from_input(input_text, ("一句话概述", "一句话简介"))
    meta_parts = [part for part in (keywords, volume) if part]
    meta = " · ".join(meta_parts) or "互动影游 · 分集剧本"

    episode_paths = sorted(
        (path for path in (root / "episodes").glob("episode-*.md") if EPISODE_FILE.search(path.name)),
        key=lambda path: int(EPISODE_FILE.search(path.name).group(1)),
    )
    if not episode_paths:
        raise SystemExit("未找到公开逐集文件")

    nav: list[str] = []
    articles: list[str] = []
    for path in episode_paths:
        match = EPISODE_FILE.search(path.name)
        number = int(match.group(1))
        node_id = f"episode-{number:03d}"
        source = path.read_text(encoding="utf-8").strip()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        title_text = episode_title(source, number)
        nav.append(
            f'<a class="episode-chip" data-jump="{node_id}" href="#{node_id}">'
            f'<span>{number:02d}</span>{html.escape(title_text)}</a>'
        )
        articles.append(
            f'<article class="episode-card" id="{node_id}" data-episode-id="{node_id}" data-source-sha256="{digest}">'
            f'<header><span class="episode-number">EP {number:02d}</span><h2>第{number}集《{html.escape(title_text)}》</h2></header>'
            f'<div class="episode-body">{render_markdown_body(source)}</div>'
            '<a class="back-top" data-jump="page-top" href="#page-top">回到顶部 ↑</a>'
            "</article>"
        )

    description = logline or "流程图预览与逐集完整剧本"
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {GENERATOR_MARKER}
  <title>{html.escape(title)}｜游戏完整剧本</title>
  <style>
    :root {{ color-scheme: light; --ink:#261e19; --muted:#796b61; --paper:#f7f1e7; --panel:#fffaf1; --line:#d7c4ae; --gold:#ad7728; --gold-soft:#f4dfb4; --blue:#346779; --shadow:0 18px 50px rgba(73,48,30,.10); }}
    * {{ box-sizing:border-box; }} html {{ scroll-behavior:smooth; }} body {{ margin:0; color:var(--ink); background:radial-gradient(circle at top left,#fff9ed 0,transparent 34rem),var(--paper); font-family:"PingFang SC","Microsoft YaHei",sans-serif; line-height:1.8; }}
    a {{ color:inherit; }} .shell {{ width:min(1180px,calc(100% - 40px)); margin:0 auto; padding:48px 0 96px; }}
    .hero {{ position:relative; overflow:hidden; padding:48px 52px; border:2px solid var(--line); border-radius:28px; background:linear-gradient(135deg,rgba(255,250,241,.96),rgba(246,230,198,.84)); box-shadow:var(--shadow); }}
    .hero::after {{ content:""; position:absolute; width:240px; height:240px; right:-70px; top:-90px; border:34px solid rgba(173,119,40,.10); border-radius:50%; }}
    .eyebrow,.section-kicker {{ margin:0 0 8px; color:var(--gold); font-size:13px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; }}
    h1 {{ position:relative; margin:0; font-family:STKaiti,KaiTi,serif; font-size:clamp(42px,7vw,78px); line-height:1.16; }}
    .meta {{ margin:18px 0 0; color:var(--muted); font-weight:650; }} .logline {{ position:relative; max-width:860px; margin:22px 0 0; font-size:clamp(18px,2.1vw,25px); font-weight:650; }}
    .top-nav {{ display:flex; gap:12px; margin:22px 0 0; flex-wrap:wrap; }} .top-nav a {{ text-decoration:none; padding:9px 15px; border:1px solid rgba(173,119,40,.35); border-radius:999px; background:rgba(255,255,255,.56); font-weight:700; }}
    .panel {{ margin-top:34px; padding:30px; border:1px solid var(--line); border-radius:24px; background:rgba(255,250,241,.88); box-shadow:var(--shadow); }}
    .section-head {{ display:flex; justify-content:space-between; gap:24px; align-items:end; margin-bottom:22px; }} .section-head h2 {{ margin:0; font-size:clamp(28px,4vw,46px); }} .section-head p {{ margin:0; color:var(--muted); }}
    .flowchart-frame {{ max-height:720px; overflow:auto; border:1px solid #dfd0be; border-radius:18px; background:#fbf7ef; }} .flowchart-frame img {{ display:block; width:100%; min-width:720px; height:auto; }}
    .script-section {{ margin-top:52px; }} .script-title {{ margin:0; font-size:clamp(34px,5vw,58px); }} .script-subtitle {{ margin:4px 0 0; color:var(--muted); font-size:18px; }}
    .episode-nav {{ position:sticky; top:0; z-index:20; display:flex; gap:10px; overflow:auto; padding:14px 0; margin:22px 0 30px; background:linear-gradient(var(--paper) 72%,transparent); scrollbar-width:thin; }}
    .episode-chip {{ flex:0 0 auto; display:flex; gap:8px; align-items:center; padding:9px 14px; border:1px solid var(--line); border-radius:999px; background:var(--panel); text-decoration:none; font-size:14px; font-weight:700; }} .episode-chip span {{ color:var(--gold); font-family:monospace; }} .episode-chip:hover {{ border-color:var(--gold); transform:translateY(-1px); }}
    .episode-card {{ scroll-margin-top:82px; margin:0 0 28px; padding:34px clamp(24px,5vw,58px); border:1px solid var(--line); border-radius:24px; background:var(--panel); box-shadow:0 12px 38px rgba(73,48,30,.07); }}
    .episode-card header {{ display:flex; gap:18px; align-items:center; padding-bottom:22px; border-bottom:1px solid #e4d7c8; }} .episode-card h2 {{ margin:0; font-size:clamp(24px,3.5vw,38px); }} .episode-number {{ flex:0 0 auto; padding:5px 10px; border-radius:8px; color:#fff; background:var(--gold); font:700 13px/1.4 monospace; }}
    .episode-body {{ max-width:880px; margin:28px auto 0; }} .synopsis {{ margin:0 0 30px; padding:18px 20px; border-left:4px solid var(--gold); border-radius:0 12px 12px 0; background:#f8ead0; }} .synopsis span {{ color:var(--gold); font-size:13px; font-weight:800; }} .synopsis p {{ margin:4px 0 0; }}
    .scene {{ margin:38px 0 10px; padding:9px 13px; border-radius:9px; background:#eee4d7; font-size:18px; }} .cast {{ margin:8px 0 24px; color:var(--muted); font-size:14px; }} .action {{ margin:14px 0; color:#4b4039; }}
    .dialogue {{ display:grid; grid-template-columns:minmax(70px,120px) 1fr; gap:16px; margin:13px 0; }} .speaker {{ color:var(--blue); font-weight:800; }} .choice-title {{ margin:30px 0 10px; color:var(--gold); font-weight:800; }} .choices {{ margin:0 0 24px; padding:16px 18px 16px 38px; border:1px dashed #cfa95e; border-radius:14px; background:#fff4d9; }} .ending {{ display:inline-block; margin:28px 0 0; padding:8px 14px; border:1px solid #b75845; border-radius:999px; color:#9f3f30; font-weight:800; background:#f9e3dc; }}
    .back-top {{ display:block; width:max-content; margin:30px 0 0 auto; color:var(--muted); font-size:14px; text-decoration:none; }} footer {{ padding:28px 0 0; color:var(--muted); text-align:center; font-size:13px; }}
    @media (max-width:680px) {{ .shell {{ width:min(100% - 22px,1180px); padding-top:20px; }} .hero,.panel,.episode-card {{ border-radius:18px; }} .hero {{ padding:30px 24px; }} .panel {{ padding:18px; }} .section-head {{ display:block; }} .dialogue {{ grid-template-columns:1fr; gap:0; }} .speaker {{ margin-bottom:1px; }} .flowchart-frame img {{ min-width:620px; }} }}
  </style>
</head>
<body id="page-top">
  <!-- EPISODE_GENERATOR_START -->
  <main class="shell">
    <header class="hero">
      <p class="eyebrow">Interactive Drama · Episode Script</p>
      <h1>《{html.escape(title)}》</h1>
      <p class="meta">{html.escape(meta)}</p>
      <p class="logline">{html.escape(description)}</p>
      <nav class="top-nav" aria-label="页面内容"><a data-jump="game-flowchart" href="#game-flowchart">游戏流程图</a><a data-jump="full-script" href="#full-script">游戏完整剧本</a></nav>
    </header>

    <section class="panel" id="game-flowchart">
      <div class="section-head"><div><p class="section-kicker">Story Map</p><h2>游戏流程图</h2></div><p>节点、选择与结局路径预览</p></div>
      <div class="flowchart-frame"><img src="./episode-flowchart.svg" alt="{html.escape(title)}游戏流程图"></div>
    </section>

    <section class="script-section" id="full-script">
      <p class="section-kicker">Full Script</p><h2 class="script-title">游戏完整剧本</h2><p class="script-subtitle">逐集剧本</p>
      <nav class="episode-nav" aria-label="逐集导航">{''.join(nav)}</nav>
      {''.join(articles)}
    </section>
    <footer>由逐集源文件确定性构建 · 不生成第二套剧本</footer>
  </main>
  <!-- EPISODE_GENERATOR_END -->
  <script>
    document.querySelectorAll('[data-jump]').forEach(function(link) {{
      link.addEventListener('click', function(event) {{
        var target = document.getElementById(link.dataset.jump);
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        history.replaceState(null, '', '#' + link.dataset.jump);
      }});
    }});
  </script>
</body>
</html>
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = args.canvas_root.resolve()
    output = root / "index.html"
    if output.is_file() and not args.force:
        existing = output.read_text(encoding="utf-8")
        if GENERATOR_MARKER not in existing and "EPISODE_GENERATOR_START" not in existing:
            raise SystemExit("index.html 不是 episode-generator 管理的页面；如确认覆盖，请使用 --force")
    output.write_text(page(root), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
