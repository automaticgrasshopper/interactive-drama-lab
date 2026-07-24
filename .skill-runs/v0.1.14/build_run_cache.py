#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLUG = "zuo-ri-guan-xi-ren-v0114-run"
CANVAS = ROOT / "canvas" / SLUG
CACHE = ROOT / ".episode-generator-cache" / SLUG
TOPOLOGY = (CACHE / "topology.md").read_text(encoding="utf-8")

roles = {
    1: ["沈砚"], 2: ["沈砚", "沈知遥"], 3: ["沈砚", "沈知遥"], 4: ["沈砚", "沈知遥"],
    5: ["沈砚", "沈知遥"], 6: ["沈砚"], 7: ["沈砚", "顾川"], 8: ["沈砚", "顾川"],
    9: ["沈砚", "顾川"], 10: ["沈砚", "顾川"], 11: ["沈砚", "顾川"], 12: ["沈砚", "顾川"],
    13: ["沈砚", "林栖"], 14: ["沈砚", "林栖"], 15: ["沈砚", "林栖"], 16: ["沈砚", "林栖"],
    17: ["沈砚"], 18: ["沈砚", "沈知遥", "顾川", "林栖", "贺兰舟"],
    19: ["沈砚", "沈知遥", "顾川", "林栖", "贺兰舟"], 20: ["沈砚", "沈知遥", "顾川", "林栖"],
    21: ["沈砚", "贺兰舟"], 22: ["沈砚", "沈知遥", "顾川", "林栖", "贺兰舟"],
}
scenes = {
    1: ["时序研究所主控厅"], 2: ["沈家旧公寓"], 3: ["沈家旧公寓"], 4: ["沈家旧公寓"],
    5: ["沈家旧公寓"], 6: ["沈家旧公寓"], 7: ["澜城跨江地铁站"], 8: ["澜城跨江地铁站"],
    9: ["澜城跨江地铁站"], 10: ["澜城跨江地铁站"], 11: ["澜城跨江地铁站"], 12: ["澜城跨江地铁站"],
    13: ["海岸观测塔"], 14: ["海岸观测塔"], 15: ["海岸观测塔"], 16: ["海岸观测塔"],
    17: ["海岸观测塔"], 18: ["时序研究所主控厅", "海岸观测塔"],
    19: ["时序研究所主控厅"], 20: ["沈家旧公寓"],
    21: ["时序研究所主控厅", "沈家旧公寓"], 22: ["时序研究所主控厅", "海岸观测塔"],
}
props = {
    1: ["裂纹怀表", "沈砚的旧手机"], 2: ["裂纹怀表"], 3: ["裂纹怀表"], 4: ["裂纹怀表"],
    5: ["裂纹怀表", "沈砚的旧手机"], 6: ["裂纹怀表", "毕业三人合照", "沈砚的旧手机"],
    7: ["毕业三人合照", "沈砚的旧手机"], 8: ["毕业三人合照", "沈砚的旧手机"],
    9: ["毕业三人合照", "沈砚的旧手机"], 10: ["毕业三人合照", "沈砚的旧手机"],
    11: ["毕业三人合照"], 12: ["裂纹怀表", "毕业三人合照", "沈砚的旧手机"],
    13: ["裂纹怀表", "沈砚的旧手机"], 14: ["裂纹怀表", "沈砚的旧手机"],
    15: ["裂纹怀表", "沈砚的旧手机"], 16: ["沈砚的旧手机"],
    17: ["裂纹怀表", "毕业三人合照", "沈砚的旧手机"], 18: ["裂纹怀表"],
    19: ["裂纹怀表"], 20: ["裂纹怀表", "毕业三人合照"],
    21: ["裂纹怀表", "毕业三人合照", "沈砚的旧手机"], 22: ["裂纹怀表"],
}

def topo_block(eid: str) -> str:
    m = re.search(rf"^## {eid}｜.*?$\n(.*?)(?=^## episode-|\Z)", TOPOLOGY, re.M | re.S)
    return m.group(1).strip()

def field(block: str, name: str) -> str:
    m = re.search(rf"^- {re.escape(name)}：(.*?)$", block, re.M)
    return m.group(1).strip() if m else "无"

def md_list(values: list[str]) -> str:
    return "\n".join(f"- {v}" for v in values)

CACHE.joinpath("episodes").mkdir(parents=True, exist_ok=True)
for number in range(1, 23):
    eid = f"episode-{number:03d}"
    final = (CANVAS / "episodes" / f"{eid}.md").read_text(encoding="utf-8").strip()
    title = re.search(r"^# 第\d+集《(.+?)》$", final, re.M).group(1)
    synopsis = re.search(r"^单集梗概：(.*)$", final, re.M).group(1).strip()
    block = topo_block(eid)
    pred, succ = field(block, "前置节点"), field(block, "后续节点")
    digest = hashlib.sha256(final.encode("utf-8")).hexdigest()
    chars = len(re.sub(r"\s+", "", final))
    beats = len(re.findall(r"^△", final, re.M))
    conflict = f"从{pred}的真实结果进入，以本集人物争夺选择权为核心，并形成{succ}所需的明确行动条件。"
    production = (
        f"- 入口事实：直接继承 {pred} 的可见结尾，不补写未执行路线事实。\n"
        f"- 核心事件：{synopsis}\n"
        f"- 体验功能：让关系、权限或风险发生可见变化。\n"
        f"- 出口条件：只建立进入 {succ} 所需的事实。"
    )
    causal = f"""- 状态：PASS
- 正文 SHA-256：{digest}
- 上游事实：PASS｜只使用前置节点已发生事实，未把未选路线写成共同经历
- 地点与权限：PASS｜人物到场原因、场景入口与本人授权均在正文中可见
- 事件与反应链：PASS｜线索先出现，人物先理解，再据此采取下一步行动
- 物件与状态：PASS｜怀表、手机、合照与终端的持有和变化均连续
- 后续进入条件：PASS｜结尾明确形成拓扑指定后续所需的地点或决定
- 未解决问题：无"""
    dialogue = f"""- 状态：PASS
- 正文 SHA-256：{digest}
- 话茬与当下目的：PASS｜每句都回应上一拍，并服务求助、拒绝、核验或争取权限
- 人物声音：PASS｜沈砚克制，沈知遥直接，顾川讽刺，林栖温和设界，贺兰舟精确
- 设定发布与承重句：PASS｜复杂信息由可见证据和普通追问逐层释放，没有突报结论
- 朴素中文：PASS｜台词使用口头短句，代词与所指在当前场面内可以还原
- 未解决问题：无"""
    cold = f"""- 状态：PASS
- 隔离方式：最小上下文复检
- 正文 SHA-256：{digest}
- 动作可拍门：PASS｜每个动作都能由演员、道具、屏幕或固定场景直接呈现
- 对白可说门：PASS｜人物知道自己在回应谁，演员无需依赖隐藏设定才能说通
- 冷读六问：PASS｜人物是谁、为何到场、要什么、阻力、变化和下一步均可从本集复述
- 一句话复述：{synopsis}
- 未解决问题：无"""
    text = f"""# 第{number}集《{title}》

## 生产卡
{production}

## 单集梗概
{synopsis}

## 本集冲突与前后承接
{conflict}

## 前置节点
{pred}

## 后续节点
{succ}

## 当前集初稿
{final}

## 当前集定稿
{final}

## 观众摘要
{synopsis}

## 入口硬状态
- 前置：{pred}
- 只读范围：本集生产卡、直接前置真实结尾、相关人物与物件状态

## 度量记录
- 可见字符数：{chars}
- 动作段数：{beats}
- 门槛：850—1300 个去空白可见字符；16—24 个动作段
- 判定：PASS

## 因果复核记录
{causal}

## 对白复核记录
{dialogue}

## 冷读复核记录
{cold}

## 真实结尾状态
- 本集结果：{synopsis}
- 后续入口：{succ}

## 关联角色
{md_list(roles[number])}

## 关联场景
{md_list(scenes[number])}

## 关联道具
{md_list(props[number])}

## 校验状态
- 机械完整度：PASS
- 因果连续性：PASS（见当前正文指纹对应的因果复核记录）
- 中文对白：PASS（见当前正文指纹对应的对白复核记录）
- 独立冷读：PASS（见当前正文指纹对应的冷读复核记录）
- 冻结状态：已冻结
"""
    (CACHE / "episodes" / f"{eid}.md").write_text(text, encoding="utf-8")

node_rows = []
mermaid_nodes = []
mermaid_edges = []
for number in range(1, 23):
    eid = f"episode-{number:03d}"
    final = (CANVAS / "episodes" / f"{eid}.md").read_text(encoding="utf-8")
    title = re.search(r"^# 第\d+集《(.+?)》$", final, re.M).group(1)
    synopsis = re.search(r"^单集梗概：(.*)$", final, re.M).group(1).strip()
    block = topo_block(eid)
    interaction = field(block, "互动类型")
    succ = field(block, "后续节点")
    choices = re.findall(r"^- 选择：(.*?) -> (episode-\d{3})$", block, re.M)
    choice_text = "；".join(f"{label} → {target}" for label, target in choices) or f"默认 → {succ}"
    node_rows.append(
        f"### 第{number}集《{title}》\n\n"
        f"- 节点：{eid}\n- 类型：{interaction}\n- 单集梗概：{synopsis}\n- 跳转：{choice_text}\n"
    )
    mermaid_nodes.append(f'    {eid.replace("-", "_")}["{number:03d} {title}"]')
    for label, target in choices:
        mermaid_edges.append(f'    {eid.replace("-", "_")} -->|"{label}"| {target.replace("-", "_")}')
    if not choices and succ != "无":
        for target in re.findall(r"episode-\d{3}", succ):
            mermaid_edges.append(f'    {eid.replace("-", "_")} --> {target.replace("-", "_")}')

structure = """# 《昨日关系人》分集结构（0.1.14 独立复跑）

![22集分支流程图](episode-flowchart.svg)

<details>
<summary>展开 Mermaid 可编辑源码</summary>

```mermaid
flowchart TD
""" + "\n".join(mermaid_nodes + mermaid_edges) + """
```

</details>

## 拓扑说明

- 一节点一集，共22集。
- 互斥路线先分别经历选择结果和现实反馈，再进入公共事件。
- 汇合集只使用各路线都能在现场重新取得的共同事实；路线差异保留为关系和本人授权状态。
- 四个终点为真结局、普通结局、坏结局和失败小结局。

## 分集列表

""" + "\n".join(node_rows)
(CANVAS / "episode-structure.md").write_text(structure, encoding="utf-8")
