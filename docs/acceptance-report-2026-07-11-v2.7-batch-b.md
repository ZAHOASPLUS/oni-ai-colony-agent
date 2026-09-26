# V2.7 批次 B 验收报告

日期：2026-07-11

结论：`mark_dig_cells` 与 `cancel_dig_cells` 已通过协议、静态、mock、负例、当前 ONI build 实机和恢复验收，批次 B 状态可由 `implementing` 改为 `verified`。V2.7 尚未完成；下一唯一实施项为批次 C。

## 验收基线

| 项目 | 值 |
| --- | --- |
| ONI | `cl-740622 release` |
| 模组 | `ONI AI Probe 0.5.0` |
| 最终安装 DLL SHA-256 | `019bed0eb797cfe760e482b76448dcc32ac7ed12c3a9d1db0396665cdaebc0d8` |
| 正式验收会话 | `session-20260711-035810-96b031a3` |
| 测试存档 SHA-256 | `e93d210c6033479fcba57c321ccc607f5616d2ab127f84ce38ea50373c14cfea` |
| 目标 cell | `135591`，`SandStone`，坐标 `(123,213)`，质量 `1840 kg` |
| 初始/最终 fingerprint | `sha256:c035b35bd0545498c73778a3ad4ccb5b3a2789dfa5eabf4c71ca3cf7b3a9eeb8` |

## 本轮已做

1. 增加 `designations.dig` 类型化状态，观测 DigTool Tiles/NaturalBackwall 过滤器、最多 32 个安全候选、现有 Diggable、稳定 instance id、进行中状态和 cell fingerprint。
2. 实现最多 32 个目标的全量预检、`DigTool.PlaceDig` 标记、精确 instance 取消、异步后置轮询、状态不变证明和失败回滚。
3. 将两项动作接入命令白名单、本地策略、协议、Bridge 摘要、运行准备器、负例注入器、验收 marker 和 `game_verify` 场景；规则层保持 observer-only，不自动创建高级动作命令。
4. 离线验证覆盖暂停态、过滤器错误、重复/非法 cell、非 tile designation、进行中 designation、错误 fingerprint、instant-dig API 禁止和验收器反向测试。
5. 实机负例 `cmd-runtime-dig-invalid-035856-c9eb9e48` 仅产生 `rejected`，分类 `behavior_fail`，没有 `accepted/executed/verified`，目标状态不变。
6. 实机 mark `cmd-runtime-dig-mark-035916-d8f53b3b` 完成 `accepted -> executed -> verified`，创建 tile-only instance `13522`；element、mass、damage、solid 均不变。
7. 实机 cancel `cmd-runtime-dig-cancel-035940-561a5fa6` 精确绑定 instance `13522`，完成 `accepted -> executed -> verified`；designation 消失且原 fingerprint 恢复。

## 发现并修复

首个会话 `session-20260711-035128-b2801706` 暴露了同步初始化竞态：`DigTool.PlaceDig` 已创建 designation，但即时 KPrefabID/flag 检查发生过早，命令误判失败；同时新对象在检查后才加入回滚集合，导致残留。失败命令为 `cmd-runtime-dig-mark-035346-8787e0e8`。

现场使用精确 cancel `cmd-runtime-dig-cancel-035419-a2831394` 清理并恢复 fingerprint，随后退出游戏。修复将 `PlaceDig` 返回对象先加入回滚集合，只同步检查最小 Diggable 形状，完整 instance/type/cell 状态交给有界后置快照验证。最终构建在全新会话从负例开始重跑通过；首会话不计入通过证据。

## 验收结果

| 门槛 | 结果 |
| --- | --- |
| C# 构建 | 通过；0 错误，保留 2 类已知 MSB3277 警告 |
| Python compileall | 通过 |
| 高级动作目录/契约 | 通过，13 项动作、8 项永久禁止项保持完整 |
| 命令安全、Bridge 可靠性、自动化、LLM 回退 | 全部通过 |
| Observer-only dig mock | 通过，未自动生成 mark/cancel |
| Batch B runtime collect | 通过，3 项必需检查全部 passed |
| `game_verify_runtime_dig.json` | 1/1 pass |
| 已知失败样例 | 1 个 `behavior_fail`，检测能力保持有效 |
| 正式会话快照 | 23 个用于收集；`probe_error=0`，JSONL 无无效行 |
| 采样性能 | 106.473 秒；P95 `4.872 ms`，最大 `8.599 ms`，队列/丢弃均 0 |
| 退出生命周期 | `session_ended` seq 39，`probe_shutdown` seq 40，`save_dirty=false` |
| 测试存档 | 长度、时间戳、SHA-256 均与运行前一致 |
| 退出策略/进程 | `actions_enabled=false`；ONI 与 Bridge 无遗留进程 |

模组差异提示在载入时出现。按用户既有授权，该提示默认允许并仅留档，不作为逐轮人工审核门槛。游戏动作本身仍逐命令经过本地策略、测试档白名单和显式批准。

本批次使用 `MinimumDurationSeconds=0` 运行专项门槛；完整 10 分钟性能门槛未在本批次重复，保留到 V2.7 最终总验收。

## 下轮要做

1. 实现批次 C 的 cell、材料 Tag、BuildingDef、Constructable 和蓝图实例状态，不先开放执行器。
2. 首个 allowlist 仅考虑空 cell 上的 `Ladder`，验证材料、方向、位置与占用契约。
3. 放置只允许 `BuildingDef.TryPlace(...)`；用静态检查永久阻断 `BuildingDef.Build(...)`、`BuildingComplete` 和 instant build。
4. 实现单体蓝图放置/精确取消，再扩展连续 utility path；每阶段都需正例、负例、恢复和当前 build 实机证据。
5. 批次 C 完成前，批次 D 的多周期计划状态机继续禁用。
