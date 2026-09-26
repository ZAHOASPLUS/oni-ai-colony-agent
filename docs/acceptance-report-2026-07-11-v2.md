# ONI AI Colony Agent V2 验收报告

日期：2026-07-11

## 验收结论

本轮结论：**通过**。

V2-0 至 V2-6 已完成实现、Dev 安装、离线回归和窗口模式 ONI 实机验收。Observer MVP、Copilot MVP 和测试存档白名单内的受限 Controlled Agent MVP 均满足方案门槛。

本结论不包含完全自动游玩、自动挖掘、建筑放置、存档修改或无人值守殖民地托管；这些能力仍属于 V2-7 或更后续范围。

## 版本与基线

| 项目 | 本轮事实 |
| --- | --- |
| ONI | `cl-740622 release` |
| 模组 | `ONI AI Probe 0.3.0` |
| 构建配置 | `Debug` |
| 最终 DLL SHA-256 | `c23b3cdf68d6622554333046680142961c7b3577c14efccc95f2ef1b4f1dec67` |
| DLL 一致性 | 构建产物、项目副本、Dev 安装目录三者一致 |
| 测试存档 | `联盟 Cycle 5.sav`，1,584,435 bytes |
| 存档 SHA-256 | `e93d210c6033479fcba57c321ccc607f5616d2ab127f84ce38ea50373c14cfea` |
| 最终实机会话 | `session-20260711-020132-dac6b835` |
| 动作策略收尾状态 | `actions_enabled=false` |
| 收尾进程状态 | ONI 0，Bridge 0 |

构建清单见 `docs/build-manifest.local.json`，测试档基线位于本机 `ai-bridge/test-save-baseline/E93D...CFEA/`。

## 本轮已做

- 完成 `oni.state.v2`、`oni.event.v2` 和 `oni.command.v2`，补齐 session、seq、build、mod、save、world、quality、TTL、前置条件、后置效果和 ack 阶段。
- 完成存档加载生命周期 patch；Cycle 4、Cycle 5 互切会产生新会话和各自精确存档哈希，不复用旧会话状态。
- 完成 Bridge 原子输出、会话隔离、stale、轮转、截断/坏行/乱序恢复、incident 生命周期和大文件预算测试。
- 完成动作默认关闭、测试档白名单、显式批准、会话/存档绑定、重复幂等和 quarantine。
- 完成 `set_paused` 与 `set_research` 的实机执行前后状态验证。
- 完成 LLM 排序输出校验、事件引用限制、成本预算和全部失败回退；真实凭据调用不是本轮硬门槛。
- 将建筑诊断改为活动世界类型化读取，并增加 30 秒缓存和换档重置，消除单次 169 ms 扫描尖峰。
- 将无效科技 ID `BasicFarming` 修正为当前游戏有效 ID `FarmingTech`。
- 更新方案状态、协议、开发说明、测试夹具、最终构建清单和 runtime/game_verify 验收包。
- 已将最终 DLL 合入 `Documents/Klei/OxygenNotIncluded/mods/Dev/oni-ai-colony-agent`。

## 实机验收

最终 runtime 报告为 `reports/runtime-acceptance.json`，总体状态 `passed`。

| 门禁 | 结果 | 本轮证据 |
| --- | --- | --- |
| Observer | 通过 | 130 个有效快照，坏 JSON 0，`probe_error` 0，序号连续，关键质量一致 |
| 性能 10 分钟 | 通过 | 644.386 秒；max 4.061 ms，P95 2.137 ms，mean 0.714 ms |
| Writer | 通过 | 最大队列深度 0，丢弃快照 0 |
| 暂停动作 1 | 通过 | `cmd-runtime-pause-020438-7aa36ad3`，`true -> false`，ack `accepted/executed/verified` |
| 暂停动作 2 | 通过 | `cmd-runtime-pause-020513-555c21b4`，`false -> true`，ack `accepted/executed/verified` |
| 研究动作 | 通过 | `cmd-runtime-research-020639-56fbc1cb`，`"" -> FarmingTech`，ack `accepted/executed/verified` |
| 重复命令 | 通过 | 重放第二个暂停 ID，只增加 `skipped_stale`，执行次数仍为 1 |
| 过期命令 | 通过 | `cmd-runtime-stale-020617-89c6cf7d` 进入 quarantine，未执行 |
| 存档安全 | 通过 | `save_dirty=false`；结束后磁盘文件长度、时间和 SHA-256 均与基线一致 |
| 正常退出 | 通过 | `session_ended` seq 217，`probe_shutdown` seq 218；ONI 与 Bridge 进程均已退出 |

建筑诊断实机来源为 `typed_active_world`，活动世界 `0`，扫描对象 `1288`。动作执行器直接读状态时缓存命中，建筑读取耗时为 0 至 0.001 ms 量级。

## 自动确认留档

用户已明确授权：ONI 模组变更/差异提示默认允许自动点击，不需要逐次人工审核，只需留档。

本轮加载 Cycle 5 时出现“允许未启用：ONI AI Probe”提示，已按授权自动点击“好的”。提示前后证据如下：

- `reports/runtime-markers/mod-change-prompt-before.png`，SHA-256 `f89f659a58441fd13b75c9be1601df8017c950eac330c9a0255366a0dc6e72bb`
- `reports/runtime-markers/mod-change-prompt-after.png`，SHA-256 `4ec3033a871cd7ce726468d71d732fce19925ae116ffc2b568f0a87ab37b2540`

该提示不再计为人工阻断；只要仍是 ONI 模组差异提示，后续按同一授权处理并记录证据。

## 离线与场景回归

| 验证 | 结果 |
| --- | --- |
| C# 构建 | 0 错误；仅保留已登记的 2 类 `MSB3277` 冲突警告 |
| Python compileall | 通过 |
| Command Safety | 4/4 通过 |
| Bridge Reliability | 5/5 通过；10,000 行回放 0.994 秒，峰值工作集 40.129 MiB |
| Automation | 9/9 通过，含 unknown-safe、瞬态不触发、恢复和建筑诊断 |
| LLM Fallback | 4/4 通过，覆盖无密钥、403、超时、无效输出、越权和预算 |
| game_verify 离线正例 | 2/2 通过 |
| game_verify runtime | 5/5 通过：observer、pause、research、safety、performance |
| known failure | 预期退出码 1，稳定分类 `behavior_fail: 1` |

## 本轮发现并修复

1. 存档加载后会话和存档哈希可能沿用旧值。增加 `SaveLoader.Load/Save` 生命周期 patch 后，跨档哈希与会话均正确切换。
2. runtime validation 使用合成 incident ID，Bridge 会拒绝为 `source_incident_not_open`。改为明确的 operator-only runtime validation 来源，生产 incident 约束保持不变。
3. JSON.NET 自动日期转换会丢失原始时区偏移，使有效命令被判为未来时间。执行器改用 `DateParseHandling.None`，并增加静态契约验证。
4. `BasicFarming` 不是当前 ONI 科技 ID。经当前程序集核对后改为 `FarmingTech`。
5. 建筑反射扫描出现 169.046 ms 尖峰。改为类型化活动世界读取与会话安全缓存后，最终 10 分钟窗口 max 降至 4.061 ms。

## 非阻断限制

- `oxygen_kg_estimate`、`water_kg_estimate`、`duplicants.stressed`、`duplicants.idle` 和 alerts 等未实现字段仍标记为 `unsupported`；依赖规则不会据此生成动作。
- 真实 LLM 服务调用未使用用户凭据验证；可靠路径仍可完全运行在 `rule_only`，失败回退已覆盖。
- 当前构建仍有 `System.IO.Compression`、`System.Net.Http` 的已知 `MSB3277` 警告，没有新增警告。
- 项目尚未创建用户确认的 Git 提交。

## 下轮要做

下一轮唯一优先级建议：`V2-7.1`，只研究“调整复制人优先级”这一项 L2 动作。

退出条件：独立命令协议、参数白名单、前后状态读取、回滚方案、专用测试档正例、过期/重复/错误目标负例和一个 known behavior failure 全部通过；在此之前 L2 保持禁用，不同时开放挖掘、建筑或调度动作。

可并行但不阻断 V2 当前结论的工作：补齐 stress、idle、氧气和水的类型化读取；在用户提供凭据并接受预算后验证真实 LLM 排序；按用户决定建立 Git 分支和提交。
