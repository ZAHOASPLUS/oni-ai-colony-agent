# V2.7 批次 C 验收报告

日期：2026-07-11

结论：`place_building_blueprint`、`cancel_building_blueprint`、`place_utility_blueprint_path` 和 `cancel_utility_blueprint_path` 已通过协议、静态安全、mock、错误目标、当前 ONI build 实机、精确恢复和 `game_verify` 验收，批次 C 可从 `implementing` 改为 `verified`。

## 验收基线

| 项目 | 值 |
| --- | --- |
| ONI | `cl-740622 release` |
| 模组 | `ONI AI Probe 0.6.0` |
| 批次 C 安装 DLL SHA-256 | `aab22fc33b67bec459eacda27f375768795c8a77fb2bec9a9f6c4299852da730` |
| 正式验收会话 | `session-20260711-052929-eaf80c15` |
| 测试档 | `娴雅的太空监牢 Cycle 6.sav` |
| 测试档 SHA-256 | `2471f42f8a3693215c9d3eab760c71b117a4ec4f1603f41445390fd7a9bfc65f` |

## 本轮已做

1. 新增 `Ladder` 和 `Wire` 类型化目录、材料可用量、候选 cell、未完工 Constructable、稳定实例 ID、路径状态及动态质量字段。
2. 放置永久限定为 `BuildingDef.TryPlace(...)`，禁止 `BuildingDef.Build(...)`、instant build、sandbox、已占用 cell、错误材料、非连续路径和部分提交。
3. 放置失败会对本命令已创建对象执行原生取消；取消只能绑定当前未开工蓝图的精确 cell/instance 组合。
4. 增加完整正负例、恢复对、运行收集器、marker、`game_verify` 场景和 destroyed Unity object 回归检查。
5. 修复取消后 Unity 已销毁对象包装仍非 CLR null、导致探针误判状态不可用的问题；所有访问现先做 Unity null 判断。
6. 修复运行汇总只读轮转后根日志尾段的问题；当前会话证据改读完整 session archive，启动补丁证据仍从根日志及其轮转文件读取。

## 实机证据

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 错误指纹 | 通过 | `cmd-runtime-blueprint-invalid-053148-79cde5a9` 仅 `rejected`，无 accepted/executed，cell 指纹不变 |
| Ladder 放置 | 通过 | cell `150199`，材料 `WoodLog`，创建 instance `9686` |
| Ladder 取消 | 通过 | 精确取消 instance `9686`，恢复空状态指纹 `sha256:c72b84...f9b8` |
| Wire 路径放置 | 通过 | cells `152108,152109,152110`，创建 instances `9687,9688,9689` |
| Wire 路径取消 | 通过 | 三个精确实例全部消失，三个原始空状态指纹逐一恢复 |
| Observer | 通过 | 144 个完整归档快照，序号连续，关键质量一致，`probe_error=0` |
| Batch C collect | 通过 | 四个必需检查全部 `passed`，使用 session archive |
| `game_verify_runtime_blueprint.json` | 通过 | `1/1 pass` |
| 已知失败样例 | 通过 | 缺失效果仍归类为 `behavior_fail` |

首轮会话 `session-20260711-051153-be2def2d` 仅作为缺陷证据：Ladder 放置成功后取消触发了已销毁 Unity 对象读取问题。修复、重建和重启后，从负例开始在全新会话重跑通过，首轮不计入成功证据。

## 安全与清理

- 动作策略仅在四个批次 C action 和精确测试档哈希下临时启用，收集结束后已恢复 `actions_enabled=false`。
- 本轮曾载入的 5 个存档均复核长度、时间戳和 SHA-256，全部保持基线值；没有执行保存。
- 模组差异提示按用户常设授权自动确认并留档，不作为逐轮人工审核门槛；游戏叶子动作仍逐命令经过本地策略和显式批准。
- 专项验收使用 `MinimumDurationSeconds=0`；随后发现旧候选刷新会产生周期性全世界扫描尖峰，该性能问题不计入批次 C 行为通过，已在最终构建 `0.6.1` 改为保留缓存和限时增量扫描，并留待 V2.7 总验收重测。

## 下轮要做

1. 实现批次 D 的 Bridge 持久计划状态机，不向游戏执行器添加计划控制 action。
2. 用不可变 revision 链、原子 current/active 指针、单 open leaf 和 verified 检查点约束推进。
3. 完成 pause/resume/abort/recover、损坏恢复、批准门禁、歧义暂停和当前会话无执行副作用实测。
4. 在最终 `0.6.1` 构建上重跑稳态性能及 A 至 D 汇总验收。
