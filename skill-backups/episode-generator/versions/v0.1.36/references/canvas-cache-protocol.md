# v0.1.30 轻量缓存协议

缓存只用于中断恢复和事实连续性，不作为公开成果。

```text
.episode-generator-cache/{slug}/
├── manifest.md
├── input.md
├── upstream-reference.md
├── episode-plan.md
├── episodes/
│   ├── episode-001.md
│   └── ...
└── continuity.md
```

`manifest.md` 固定写入 `episode-cache-v0.1.30`。

每集记录：

- 分集编号、标题和梗概；
- 当前完整剧本；
- 正文 SHA-256；
- 状态：`待写作`、`已写作`、`已口语化`；
- 只从正文提取的简短事实摘要。

`continuity.md` 只记录当前全剧正文指纹、事实问题和受影响分集。正文指纹改变时，“事实一致”自动失效。

公开文件只能在全部分集 `已口语化` 且全剧状态为 `事实一致` 后组装。缓存、事实摘要、修复记录和状态字段不得进入公开文件。
