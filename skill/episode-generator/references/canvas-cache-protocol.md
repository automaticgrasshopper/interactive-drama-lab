# 画布缓存与滚动窗口协议

本文件规定分集规划师如何使用 Canvas 文件、隐藏缓存、滚动窗口和校验回写。只在运行环境开放 `ls / glob / read_file / write_file / edit_file / grep / exec` 时执行。

## 一、项目定位

1. 用 `ls` 查看当前 workspace。
2. 用 `glob` 查找 `**/canvas/*/index.html`。
3. 唯一候选直接采用；多个候选依次按游戏标题 slug、当前任务关联文件和最近更新时间自动选择。
4. 没有候选时，在当前 workspace 的 `canvas/` 下创建游戏标题 slug 目录和基础文件。
5. 将选择或创建依据写入 manifest，不在后续步骤重新猜测，也不为路径选择询问用户。

## 二、目录初始化

使用 `exec` 创建：

```text
{canvas-root}/.episode-cache/episodes/
```

使用 `write_file` 创建不存在的缓存文件；已有文件必须先 `read_file`，再使用 `edit_file`。

公开文件：

```text
{canvas-root}/episode-structure.md
{canvas-root}/episode-script.md
```

缓存文件：

```text
{canvas-root}/.episode-cache/manifest.md
{canvas-root}/.episode-cache/global.md
{canvas-root}/.episode-cache/topology.md
{canvas-root}/.episode-cache/characters.md
{canvas-root}/.episode-cache/props.md
{canvas-root}/.episode-cache/hooks.md
{canvas-root}/.episode-cache/validation.md
{canvas-root}/.episode-cache/episodes/episode-NNN.md
```

## 三、缓存格式

### manifest.md

固定栏目：

```text
# 项目清单

## 项目
## 缓存版本
## 当前阶段
## 节点总数
## 已完成分集
## 对白校验线程
## 连续性校验线程
## 最近更新
```

缓存版本使用 `episode-cache-v0.3`。

### global.md

固定栏目：

```text
# 全局事实

## 故事前提
## 事实基线
## 世界规则与代价
## 主干情绪脊
## 结局条件
## 场景固定连接
## 全局未解决冲突
```

### topology.md

每个节点记录编号、标题、入边、出边、互动类型、选择文字、目标节点和结局状态。流程图从此文件生成，不从梗概重新推断。

### characters.md

先记录静态事实，再按路径记录动态状态：

```text
## 人物名
### 静态事实
### 当前路径状态
- 当前节点：
- 当前目标：
- 已知信息：
- 当前误解：
- 隐瞒信息：
- 关系状态：
- 权力与筹码：
- 已付代价：
- 弧光阶段：
```

### props.md

每件关键物件记录：

```text
## 物件名
- 初始来源：
- 当前持有人：
- 当前地点：
- 当前状态：
- 知道其作用的人：
- 最近出现节点：
- 待回收节点：
```

### hooks.md

每个钩子记录抛出节点、具体问题、当前已知、部分回答节点、最终回收节点和当前状态。

### episode-NNN.md

固定栏目：

```text
# 第N集《标题》

## 生产卡
## 单集梗概
## 本集冲突与前后承接
## 前置节点
## 后续节点
## 当前集初稿
## 当前集定稿
## 真实结尾状态
## 下游自然参数
## 校验状态
```

## 四、滚动窗口

### 普通线性节点

读取：

- `global.md` 压缩事实和情绪方向；
- 当前集生产卡；
- 直接前置集真实结尾；
- 直接后续集进入条件；
- 当前人物、物件和钩子状态；
- 必要时前一集梗概。

### 互动目标节点

额外读取产生该选择的前置节点、原始选项动作和选择瞬间状态。

### 汇合节点

读取所有直接前置集的真实结尾和各路线必须保留的差异，不以编号最近的一集代替其它来路。

### 结局节点

读取开放该结局的关键选择、证据、人物关系、资源和代价状态。

## 五、逐集回写

每集通过校验后：

1. 将定稿和真实结尾写入当前分集文件。
2. 更新 `characters.md` 中受影响人物的路径状态。
3. 更新 `props.md` 中受影响物件的持有、位置和状态。
4. 更新 `hooks.md` 中新抛出、部分回答或已回收的钩子。
5. 更新直接后续集生产卡中的前置真实结尾。
6. 更新 `validation.md` 和 `manifest.md`。
7. 冻结当前集，再继续下一节点。

## 六、确定性校验

使用 `exec` 检查：

- 节点编号唯一；
- 所有目标节点存在；
- 第一集是唯一入口；
- 所有节点可达；
- 结局无出边；
- 非结局有默认推进或选择；
- 分集缓存文件数量与节点数一致；
- 所有关键物件有来源和当前状态；
- 所有非终局钩子有计划回收节点；
- 用户完整稿节点数与拓扑一致。

脚本只检查确定性问题，不替代中文对白和叙事连续性的语义判断。

## 七、公开层更新

`episode-structure.md` 由冻结的 `topology.md` 和生产卡投射生成。

`episode-script.md` 由全部已冻结的分集定稿按编号组装。组装阶段不重新生成全文。

只有 `index.html` 中存在明确且唯一的自动内容标记区时，才用 `edit_file` 同步公开内容；没有标记区时保留两个 Markdown 公开文件，不改动页面结构。

## 八、恢复

新会话或中断后：

1. 定位 Canvas。
2. 读取 `manifest.md` 判断当前阶段。
3. 读取对应全局缓存和未完成节点。
4. 从第一个未冻结节点继续。
5. 不重做已冻结节点，除非全剧校验确定其在受影响范围内。
