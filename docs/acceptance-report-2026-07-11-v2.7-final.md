# V2.7 最终统一验收报告

日期：2026-07-11

结论：**通过。** `oni-ai-colony-agent` 在本方案定义的 Observer、Copilot、受限 Controlled Agent 和 V2.7 A-D 高级动作范围内完成最终验收。完全自动游玩、长期无人值守托管和表外游戏动作仍不属于本次交付。

## 验收基线

| 项目 | 最终值 |
| --- | --- |
| Git 分支 / 验收前 HEAD | `codex/oni-ai-colony-agent-v2` / `f18a07f` |
| ONI build | `cl-740622` |
| Probe | `0.6.8` |
| DLL SHA-256 | `adbe27d6a5854753245adeb3de694f62bb5ccd5924b0344df6f210484a4c6749` |
| 运行 ID | `v27-hl-068-20260711-1854` |
| 目标存档 | `ONI-AI-V27-HL-068-20260711-1854.sav` |
| 最终会话 | `session-20260711-195030-f68d8d52` |
| 最终存档 tip | `sha256:d959ed029faee2b4e30dfe2c36250a5ecb51252e0d1a4203a8571ff475a52808` |
| lineage tip | `sha256:3add6e69c1372f1ca6797023eecc05a20a37a787f9f3ef03d8d40b828e46676b` |

## 完成度

| 范围 | 完成度 | 结论 |
| --- | ---: | --- |
| V2-0 至 V2-6 | 100% | 静态、fixture、Bridge、规则、命令安全、LLM 回退均通过 |
| V2-7 A-D 动作目录 | 13/13 | 全部为 `verified`，永久禁止项保持不可达 |
| 单存档运行检查点 | 14/14 | 精确顺序、逐项显式保存/重载、冻结产物和 hash chain 全通过 |
| 最终 runtime checks | 15/15 | 含 `save_lineage_runtime` 聚合门 |
| `game_verify` | 11/11 正例 | known failure 另按预期得到 1 个 `behavior_fail` |
| CLI 委派 | 3/7 = 42.86% | 三项 cheap worker 审查均完成、复核并批准 |
| 完成审计 | 20/20 | 含磁盘 tip、marker 绑定、策略关闭和无残留进程 |

方案定义范围总完成度为 **100%**。

## 本轮已做

1. 在 Probe `0.6.8` 下创建唯一新存档 lineage，串行完成 observer、暂停、研究、复制人优先级、挖掘、建筑蓝图、安全负例、计划控制面和性能共 14 个检查点。
2. 每项检查点均证明非 autosave 显式保存、同一路径、保存 hash 轮换、新 session 重载、磁盘/事件/快照 hash 一致、策略关闭和队列静默。
3. 证明零库存缺料蓝图：Ladder 使用 `WoodLog=[0.0]` 放置于 cell `138741`；Wire 使用 `Cinnabar=[0.0]` 放置于 cells `139386,139387`。两者均创建未完工等待配送蓝图，再按 exact instance 取消并恢复原 fingerprint。
4. 完成 Colony Plan create、advance、pause、resume、recover、abort 控制面链；候选命令未获批准、未执行，最终进入 quarantine。
5. 运行 11 个正向 `game_verify` 场景；构造的缺失状态变化反例稳定返回退出码 1 和 `behavior_fail`。
6. 正常退出 ONI，记录当前会话 `session_ended` seq 303 和 `probe_shutdown` seq 304；随后停止 Bridge。普通动作、建议、验收控制、pending、approved 和 inflight 均为 0。

## 性能证据

性能检查绑定到最终检查点保存前会话 `session-20260711-193125-9e1bd5ac`：

| 指标 | 结果 | 门槛 |
| --- | ---: | ---: |
| 连续时长 | `1136.231 s` | `>= 600 s` |
| 快照数 | `227` | 有连续样本 |
| P95 | `4.956 ms` | `<= 20 ms` |
| 最大耗时 | `6.532 ms` | `<= 50 ms` |
| 最大 writer queue | `0` | `< 100` |
| 丢弃快照 | `0` | `= 0` |

## 审查后优化

本轮在通过初始 runtime 后又执行三项独立 CLI 审查，并落实以下收紧：

- 蓝图成功门必须显式绑定 observed `materials_available=false` 和全零 `available_mass_kg`；库存充足 fixture 会被拒绝。
- 最终 lineage 必须与规定的 14 项顺序完全一致，不能只做集合包含检查。
- 每项 checkpoint 必须至少冻结一个产物，且产物必须位于本 lineage artifact 根目录并通过长度与 SHA-256 校验。
- 每项运行检查绑定自己的 checkpoint-before session；重复命令分别绑定原执行会话和重放会话。
- 禁用策略必须撤销全部 save hash；每次显式 checkpoint save 都必须轮换 hash。
- runtime marker 升级为 v2，绑定本轮 run id、lineage tip、当前 save tip 和 runtime report SHA-256。
- 完成审计要求精确的 11 场景 ID、生成时间晚于当前 runtime report，并在关闭态改用“离线完整链 + 磁盘 tip + 无进程”三重校验。

收紧后的现有证据重放一次通过，没有重新执行或伪造游戏动作。

## 最终门禁

`verify_v27_completion.py --require-clean-processes` 的 20 个必需检查全部通过：目录完整性、静态报告、live plan、命令安全、Bridge 可靠性、LLM 回退、最终 runtime、lineage fixture、工作流、正负 `game_verify`、DLL hash、版本、策略关闭、有序 lineage、磁盘 tip、marker 绑定、CLI 40% 覆盖和进程清理。

关闭后目标 `.sav` 的磁盘 SHA-256 仍等于 manifest tip。Player.log 关闭尾部没有 Probe error；仅有一条 KleiMetrics 网络发送重试 warning，与模组行为无关。

## 剩余风险

1. **低：**损坏或不可解析的 `reload-intent.json` 当前会重复记录恢复错误并让外部 CLI 超时，尚未转成确定的 failed/quarantine 终态。正常 intent、保存和高层重载路径已通过本轮实机证据。
2. **流程：**三个 cheap worker 的有效输入均超过各自任务预算。结果经主线程复核后仍有效，但调度器目前没有硬预算停止门。

以上均不阻断本轮定义范围交付。

## 下轮要做

1. 为 corrupt `reload-intent.json` 增加确定终态、inflight 清理、crash/restart 与损坏负例，再使用新 run id 和新存档实测。
2. 为 Fleet 增加有效输入 token 硬预算或自动缩小上下文；保持 cheap-first，禁止自动高价回退。

下轮不扩展无人值守游戏动作范围，也不改写本轮冻结 evidence。

## 证据入口

- `reports/runtime-acceptance.json`
- `reports/v27-completion-audit.json`
- `reports/save-lineage-verification.json`
- `reports/game-verify/v27-final-positive-20260711/results.json`
- `reports/game-verify/v27-final-known-failure-20260711/results.json`
- `docs/cli-v27-20260711/delegation.json`
- `ai-bridge/evidence/save-lineage/v27-hl-068-20260711-1854/manifest.json`
