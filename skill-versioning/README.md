# episode-generator 版本管理

## 目标

每次修改 Skill 前先保存不可覆盖的完整快照。每个快照包含全部文件的 SHA-256 清单、父版本、变更摘要、来源和时间；恢复前自动复制当前 Skill 到安全目录，并把恢复事件追加到历史日志。

## 固定工作顺序

1. 修改前执行 `snapshot`，版本号必须与 `SKILL.md` 内部标记一致。
2. 执行修改并提升版本号。
3. 再执行 `snapshot` 保存新版本。
4. 执行 `verify`，校验全部文件哈希。
5. 需要回退时执行 `restore`；不得手工覆盖版本目录。

## 常用命令

```bash
python3 skill-versioning/version_episode_generator.py list
python3 skill-versioning/version_episode_generator.py verify v0.1.19
python3 skill-versioning/version_episode_generator.py snapshot --version v0.1.21 --parent v0.1.20 --summary "本版变更摘要"
python3 skill-versioning/version_episode_generator.py restore v0.1.19
```

以上命令在 `interactive-drama-lab` 目录执行。`restore` 会先把当前版本保存到 `skill-backups/episode-generator/versions/safety/`，因此回退本身也不会抹掉回退前状态。

## 保存位置

- 版本快照：`skill-backups/episode-generator/versions/<版本号>/`
- 逐文件哈希与版本元数据：每个快照内的 `VERSION.json`
- 追加式修改/恢复历史：`skill-backups/episode-generator/versions/history.jsonl`
- 旧式里程碑压缩包：`skill-backups/*.tar.gz`

## 当前可精确恢复范围

已按逐文件哈希验证：v0.1、v0.2、v0.3、v0.4、v0.5、v0.6、v0.1.7—v0.1.14、v0.1.19、v0.1.20。

历史上 v0.1.15—v0.1.18 没有留下完整发布快照，现有会话补丁不足以保证逐字节复原，因此没有伪造这四个版本。相关阶段仍保留在旧式里程碑压缩包和会话修改记录中，但不能宣称可精确恢复。自 v0.1.19 起，所有新版本必须执行上述双快照流程，版本号不得再出现空档。
