# ONI AI Colony Agent V2.7 Batch A 验收报告

验收日期：2026-07-11

## 结论

V2.7 完整高级动作表已冻结为 4 个批次、13 个动作和 8 类永久禁止能力。批次 A `set_duplicant_chore_priority` 已通过静态、mock、已知失败、当前 ONI build 实机正例、反向恢复和错误身份负例，状态可由 `implementing` 改为 `verified`。批次 B 至 D 仍未完成且保持默认禁用。

## 本轮基线

| 项目 | 结果 |
| --- | --- |
| ONI | `cl-740622`，`release` |
| 模组 | `ONI AI Probe 0.4.0` |
| 安装 DLL SHA-256 | `2cbb9da8054cf4c4c898101c3b4a02e925f9eb81c1289165fc38766dfd5ddb25` |
| 测试档 | `联盟 Cycle 5.sav` |
| 测试档 SHA-256 | `e93d210c6033479fcba57c321ccc607f5616d2ab127f84ce38ea50373c14cfea` |
| 运行会话 | `session-20260711-032208-02155d5d` |

构建产物、项目根副本和 Dev 安装 DLL 的哈希完全一致。测试前后存档长度、修改时间和 SHA-256 均未变化。

## 本轮已做

1. 建立 `advanced-actions.v1.json` 与人读动作表，覆盖 A 至 D 四个能力族；表外动作不得进入执行器。
2. 反编译当前 DLL 核证 `ChoreConsumer`、`DigTool`、`Diggable`、`BuildingDef.TryPlace`、取消事件和多周期恢复锚点。
3. 新增活动世界复制人的稳定 `KPrefabID.InstanceID`、名称和 16 个可调 ChoreGroup 优先级观测，动态路径质量均为 `observed`。
4. 新增批次 A 类型化执行器，强制暂停态、实例加名称双重绑定、原值绑定、`0..5`、单目标、trait 和后置状态门禁。
5. 新增独立 Batch A 运行收集器以及错误名称负例；任何错误目标执行确认都会使验收失败。
6. 将模组提升到 `0.4.0` 并安装；动作策略仅在实测窗口启用，结束后已恢复 `actions_enabled=false`。

## 实机证据

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| Observer | 通过 | Cycle 5、暂停、5 名复制人、66 个可信快照、无 probe error |
| 正向修改 | 通过 | `cmd-runtime-priority-032556-cc1964c3`，实例 `12018` 的 `Dig`：`3 -> 4`，ack `accepted/executed/verified` |
| 反向恢复 | 通过 | `cmd-runtime-priority-032622-3960fdc3`：`4 -> 3`，ack `accepted/executed/verified` |
| 错误身份 | 通过 | `cmd-runtime-priority-invalid-032710-ea401219`，仅 `rejected`，分类 `behavior_fail`，无执行 ack，值保持 `3` |
| Runtime marker | 通过 | `game_verify_runtime_priority.json` 同时验证修改/恢复和错误目标标记 |
| Known failure | 通过 | 缺少预期状态变化的样本继续被分类为 `behavior_fail` |
| 正常退出 | 通过 | `session_ended` seq 81、`probe_shutdown` seq 82，`save_dirty=false` |

Batch A 会话运行约 324 秒，采样 p95 `2.277 ms`、最大 `5.583 ms`、队列最大深度 `0`、丢弃快照 `0`。本轮未重复执行完整十分钟性能门禁；该门禁留到 V2.7 全批次最终验收重新执行。

## 审计修正

并行只读审计发现原目录把建筑蓝图入口写成 `BuildingDef.Build(...)`。该方法会直接生成 `BuildingComplete`，违反禁止即时建造的不变量。目录现已改为 `BuildingDef.TryPlace(...)`，并把直接完工建筑生成列为永久禁止项。

批次 B 还存在两个明确阻断：`DigTool.PlaceDig` 依赖 Tiles/Backwall UI 过滤状态；`CancellableDig` 取消需要等待缩小动画后对象才删除。两项在解决和验证前不得开放。

## 下轮要做

当前唯一优先级是批次 B：

1. 观测活动世界 cell、自然实体、foundation、Constructable、现有 Diggable 和 DigTool 过滤状态。
2. 决定并验证过滤状态绑定方案；若不能可靠消除 UI 状态依赖，则保持 `mark_dig_cells` 阻断。
3. 实现最多 32 个 cell 的全量预检、精确新建集记录、异步取消轮询和同会话恢复。
4. 跑单 cell、批量、重复目标、混合非法目标、错误世界、取消竞态、known behavior failure 和当前 build 实机闭环。

批次 C、D 不并行进入执行器；动作策略当前关闭，ONI 与 Bridge 均已停止。
