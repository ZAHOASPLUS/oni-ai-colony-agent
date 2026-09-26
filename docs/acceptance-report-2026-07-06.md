# ONI AI Colony Agent 验收报告

日期：2026-07-06

## 验收结论

本轮验收结论：有条件通过。

项目已完成本地 ONI 开发模组、JSONL 探针、Bridge 聚合、规则建议、命令队列、LLM 计划层骨架、自动化 mock 场景和 Dev mod 安装链路。自动化验收全部通过；最新一轮未重新启动 ONI，因此新增的建筑诊断字段仍需下一轮实机加载存档后确认。

## 验收范围

- 模组构建：`mod/OniAiProbeMod.csproj`
- 本地安装：`%USERPROFILE%\Documents\Klei\OxygenNotIncluded\mods\Dev\oni-ai-colony-agent`
- 探针输出协议：`ai-bridge/state.jsonl`
- Bridge 输出：`latest_state.json`、`state_summary.md`、`advice.md`、`advice.json`、`plan.json`
- 命令队列：`commands/pending`、`commands/approved`、`commands/done`、`commands/failed`
- 自动化验证：`scripts/verify_automation.py`、`game_verify` 场景

## 本轮已做

- 搭建 `ONI AI Probe` 本地开发模组，包含 Harmony 生命周期 patch、周期性状态快照、错误事件、退出事件。
- 建立本地 Bridge，支持读取 JSONL、压缩最新状态、生成摘要、建议、计划和待审批命令。
- 建立规则 Agent，覆盖低食物、低氧警报、复制人 idle、无研究、核心建筑缺失等建议。
- 建立受限命令队列，支持 `focus_note`、`set_paused`、`set_research`、`noop`，并要求人工审批后由游戏内执行器读取。
- 接入 LLM 计划层骨架，默认 `rule_only`，可通过显式参数启用。
- 修正食物读数：从活动世界库存 `GameTags.Edible` 可拿取物读取 kcal，避免把世界中全部 `Edible` 组件误计入食物。
- 增强建筑读数诊断：新增 `_source`、`_active_world_id`、`_sample_ids`、`_top_prefabs`，并让摘要展示扫描来源和 prefab 样本。
- 收紧建筑规则误报：建筑扫描不可用时不再把 `0` 当成确定缺失。
- 增加 `building_diagnostics` mock 场景，验证摘要中保留建筑扫描诊断。
- 已将模组安装到 Dev mod 目标目录。

## 验收结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| C# 模组构建 | 通过 | `build-mod.ps1` 成功，0 错误 |
| C# 构建警告 | 可接受 | 仍有 `System.IO.Compression` / `System.Net.Http` 版本冲突警告，未新增阻断错误 |
| Python 编译 | 通过 | `py -3 -m py_compile` 成功 |
| 项目 mock 验证 | 通过 | `verify_automation.py` 返回 `ok: true`，5 个场景全通过 |
| game_verify 项目场景 | 通过 | `total: 2`，`pass: 2` |
| 已知失败分类 | 通过 | bundled failure scenario 分类为 `behavior_fail: 1` |
| Dev mod 安装 | 通过 | `OniAiProbeMod.dll` 已在 Dev 目录，时间 `2026/7/6 21:56:10` |
| ONI 进程状态 | 通过 | 验收结束时未发现 `OxygenNotIncluded` 进程 |

## 历史实机证据

上一轮已启动 ONI U59 并加载存档，探针写出 `state_snapshot`，Bridge 成功生成摘要和建议。最后实机状态显示：

- `read_quality`: `partial_reflection_v2`
- `cycle`: 4
- `duplicants.total`: 5
- `resources.food_kcal`: 0
- `buildings._scanned`: 2437
- `probe_shutdown` 正常写出

该实机证据验证了模组加载、状态写出、食物读数修正和 Bridge 刷新；但它发生在新增建筑诊断字段之前。

## 当前风险

- 新增建筑诊断字段尚未经过实机加载存档验证。
- 核心建筑计数目前仍可能为 0，需要用 `_sample_ids` 和 `_top_prefabs` 判断是存档确实缺失，还是 prefab 识别仍需扩展。
- `set_research` 仍是反射尝试型动作，尚未完成实机成功设置研究的行为验证。
- 当前项目目录仍处于未跟踪状态，尚未 commit。
- 真实 LLM 调用此前返回 403，当前验收只覆盖 `rule_only` 模式。

## 下轮要做

1. 启动 ONI，加载同一存档，确认新快照包含 `_source`、`_active_world_id`、`_sample_ids`、`_top_prefabs`。
2. 用新建筑诊断判断核心建筑计数为 0 的原因，并按实际 prefab 名补充匹配表或规则策略。
3. 做 `set_research` 实机命令验收：生成 pending、人工 approve、游戏内执行、确认 `command_ack` 和文件移动结果。
4. 将实机建筑诊断和命令执行结果固化为新的 game_verify 场景或半自动验收脚本。
5. 复盘 LLM 403 原因，在不打印密钥的前提下补充配置健康检查和失败原因摘要。
6. 视需要创建分支并提交当前项目快照。
