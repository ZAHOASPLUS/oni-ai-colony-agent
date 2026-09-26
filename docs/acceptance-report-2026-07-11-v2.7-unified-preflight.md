# V2.7 统一新存档最终验收：预检报告

日期：2026-07-11

结论：**`0.6.8` 预检和部署通过，最终验收未完成。** 源码、Release 构建、
Dev 安装、保存生命周期、单存档检查点链和完整静态回归已经就绪，隔离
运行 ID 为 `v27-hl-068-20260711-1722`。`0.6.5` 的低层重载已按
`runtime_fail` 留档；`0.6.7` 探索谱系完成了 7/14 个检查点，但它暴露出
缺料蓝图门禁错误，不能作为 `0.6.8` 最终通过证据。

## 本轮已做

| 项目 | 状态 | 当前证据 |
| --- | --- | --- |
| 高级动作总表 | 通过 | 13 项动作、A-D 四批次、8 类永久禁止能力保持完整 |
| 研究动作 | 通过（源码/静态） | 精确清空并设置目标队列，`SetActiveResearch(..., true)` |
| 浮点前置条件 | 通过（源码/静态） | 整数保持精确；float 使用 `1e-6 * scale` 相对容差 |
| 周期性能修复 | 通过（源码/静态） | 快照记录由 writer thread 流式 JSON 输出；主线程不再创建完整大字符串；未跟踪 dig designation 上限 32 |
| 保存完成钩子 | 通过（源码/构建） | Save postfix 验证路径、重写、hash、ONI 存档头和活动指针；失败保持 `save_dirty=true` |
| 高层重载控制 | 通过（源码/构建） | `LoadScreen.ForceStopGame()` 后设置活动路径并调用 `SaveLoader.LoadScene()`；控制器默认关闭且精确绑定 run/session/save/path |
| 缺料蓝图语义 | 通过（源码/静态） | 允许 `TryPlace` 创建等待配送蓝图；不生成/消耗资源，合法材料、位置、暂停、非沙盒、回滚和精确取消门禁保留，直接 `Build` 永久禁止 |
| 单一新存档 lineage | 通过（fixture） | reserve/activate/checkpoint/verify、命令文件 hash、策略关闭、空 pending/approved、篡改拒绝全部通过 |
| 当前 hash 策略轮换 | 通过（fixture） | 启用时只允许 lineage tip 的一个 hash；禁用时撤销全部 hash；队列非空拒绝切换 |
| Release 构建 | 通过 | 0 errors；仅保留两类已登记 `MSB3277` 警告 |
| 源码/Release DLL | 通过 | `sha256:adbe27d6a5854753245adeb3de694f62bb5ccd5924b0344df6f210484a4c6749` |
| Dev 安装 DLL | 通过 | 与 Release DLL hash 一致，版本 `0.6.8` |
| 安全源档 | 通过 | `ONI-AI-V27-FINAL-20260711-0825.sav`，`sha256:00099a20cbbc03db0f6dc32dafec3697207de7580f6f63d2fa00f6f0de6bcdda` |

本轮通过的自动检查：

- `verify_advanced_actions.py`
- `verify_automation.py`
- `verify_bridge_reliability.py`
- `verify_colony_plan.py`
- `verify_command_safety.py`
- `verify_llm_fallback.py`
- `verify_save_lineage.py`
- `verify_validation_checkpoint.py`
- `verify_v27_workflow.py`
- `python -m compileall -q agent bridge scripts`
- `dotnet build mod/OniAiProbeMod.csproj -c Release`
- `game_verify_static_probe.json`（通过）和 known behavior failure（按预期分类为 `behavior_fail`）

## 仅作历史证据

- 批次 A 的优先级正向、恢复和错误身份负例。
- 批次 B 的 dig mark、exact instance cancel 和错误 fingerprint 负例。
- 批次 C 的 Ladder/Wire 放置、精确取消和错误目标负例。
- 批次 D 的计划 revision、批准门禁、恢复和 abort。
- 旧 Probe 会话的 observer、pause、research、safety 和 10 分钟性能结果。
- `0.6.7` 探索运行 `v27-hl-20260711-1611` 的前 7 个检查点：它们证明高层保存/重载链可工作，并定位了 Batch C 库存误门禁，但不能并入 `0.6.8` 最终谱系。

上述证据证明实现路径可行，但它们来自不同旧存档或旧模组版本，不满足
“本轮所有游戏验证统一在一个新存档”的范围要求。

## 当前未完成

1. 启动 ONI，由用户手动载入一次安全源档 `ONI-AI-V27-FINAL-20260711-0825`；不使用 Computer Use，模组差异提示按常设授权默认允许并留档。
2. 创建此前不存在的目标 `.sav`，通过 `0.6.8` 验收控制器执行另存和高层重载。
3. 在该存档完成 14 个规定场景；每个场景后由控制器显式保存、重载并记录检查点。
4. 将 600 秒性能检查绑定到性能场景保存前 session，证明 P95 <= 20 ms、max <= 50 ms、queue < 100、drop = 0。
5. 重新生成本轮 runtime markers、11 项正例 game_verify 和 1 项 known `behavior_fail`。
6. 禁用策略、停止 ONI/Bridge，执行 V2.7 completion auditor。
7. CLI worker 结果仍为 `infra_fail`；除非出现可用模型 API key，否则不得虚报 40% 已完成。

## 已登记失败

- Probe `0.6.5` 运行 `v27-final-bg-20260711-1515` 使用低层 `SaveLoader.Load`，虽产生新 session 并核对到目标 hash，但绕过场景卸载后出现大量 `NullReferenceException` 和 handle version 错误。
- 失败测试档 `ONI-AI-V27-BG-20260711-1515.sav` hash 为 `sha256:bcbe4cd3400b3d0d93419e0b2e00cf2cff7749f0fdb93ac52767aeb6b2cfa9d5`。
- 证据保存在 `reports/runtime-failures/20260711-low-level-reload.json`；该谱系只用于回归与失败分类，不得激活为最终验收谱系。

## CLI 委派

三个只读审查包占最终阶段工作包的 `3/7 = 42.86%`，任务清单、提示词、
受保护凭据读取器、项目专用 Fleet 配置和三个低成本 worker home 已准备完成。
Fleet Doctor 通过，但当前令牌文件中是 46 字符的管理面板会话令牌；Dali
控制台明确说明模型调用必须使用令牌管理页创建的 `sk-` API 密钥。Dali 与
备用 Responses 路由的最小 ProviderCheck 均失败，因此当前
`completed_by_cli=0/3`。失败只作为 `infra_fail` 证据，不能计入 40% 完成比例。
详见 `docs/cli-delegation-2026-07-11-v27-final.md`。

## 下轮要做

启动已安装 `0.6.8` 的 ONI 并手动载入一次安全源档；随后创建并激活全新的
高层重载 lineage，按 14 场景串行验证、完成 600 秒性能、运行 game_verify
与最终审计。任何一步保存、hash、session、策略或命令证据不一致时立即停止，
不带病进入下一检查点。
