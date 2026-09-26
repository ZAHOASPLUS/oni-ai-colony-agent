# V2.7 高级动作总表

本文冻结 V2.7 的完整动作范围。这里的“完整”指实施方案原定的四个能力族：复制人工作优先级、挖掘标记、低风险建筑蓝图、多周期计划与恢复；不等于覆盖 ONI 的每个 UI 操作。

机器可读真相源为 `config/advanced-actions.v1.json`。表外动作不得临时加入执行器，必须先修改目录、风险级别、协议和验收门槛。

## 全局硬门槛

1. 游戏动作默认关闭，只能作用于显式白名单测试档。
2. 每个游戏叶子动作必须绑定当前 session、save、world、source snapshot 和 state hash，并逐条人工批准。
3. 高级游戏动作执行时必须有 `game.paused=observed true` 前置条件；动作本身不得偷偷暂停或恢复游戏。
4. 每个动作必须有可信执行前状态、强参数白名单、后置状态证明和可审计结果文件。
5. 批量动作先完整验证全部目标，再执行；不允许“执行到一半才发现后续目标非法”。
6. 回滚也是新命令，不能绕过批准、TTL、会话、存档或后置验证。
7. 多周期计划只能编排目录内的叶子动作，不能直接调用游戏 API，也不能自动批准。
8. unknown、unsupported、error、stale 或目标歧义一律停止，不做最佳猜测。

## 动作表

| 批次 | Action ID | 执行面 | 等级 | 单命令上限 | 成功证据 | 回滚 | 当前状态 |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| A | `set_duplicant_chore_priority` | 游戏 | L2 | 1 个复制人/工作组 | 指定实例的指定 ChoreGroup 从 `from_priority` 变为 `priority` | 同动作交换前后值 | 已验证 |
| B | `mark_dig_cells` | 游戏 | L2 | 32 cells | 每个目标 cell 出现本命令创建的 Diggable | 对精确创建集执行取消 | 已验证 |
| B | `cancel_dig_cells` | 游戏 | L2 | 32 cells | 每个目标 cell 的既有 Diggable 消失 | 仅对执行前已标记项重新标记 | 已验证 |
| C | `place_building_blueprint` | 游戏 | L3 | 8 个 | 每个目标产生指定 def/material/orientation 的 Constructable | 按新实例 ID 取消 | 已验证 |
| C | `cancel_building_blueprint` | 游戏 | L2 | 8 个 | 指定未完工 Constructable 消失 | 仅在原完整契约仍有效时重放 | 已验证 |
| C | `place_utility_blueprint_path` | 游戏 | L3 | 64 cells | 连续路径上产生指定 utility def 的蓝图集合 | 按记录的蓝图实例集取消 | 已验证 |
| C | `cancel_utility_blueprint_path` | 游戏 | L2 | 64 cells | 该 path ID 记录的未完工蓝图消失 | 仅按原路径契约重放 | 已验证 |
| D | `create_colony_plan` | Bridge | L2 | 1 个计划 | 写出带 revision、依赖、检查点和叶子动作的 draft | 未启动可 abort；已启动需隔离 pending | 已验证 |
| D | `advance_colony_plan` | Bridge | L3 | 1 个步骤 | 只创建一个 dependency-ready pending 叶子命令 | 使用叶子动作回滚契约 | 已验证 |
| D | `pause_colony_plan` | Bridge | L2 | 1 个计划 | 不再产生新命令，已有 approved 不被改写 | 重新校验后 resume | 已验证 |
| D | `resume_colony_plan` | Bridge | L2 | 1 个计划 | session/save/world/检查点全部重新通过 | pause | 已验证 |
| D | `abort_colony_plan` | Bridge | L2 | 1 个计划 | 状态变为 aborted，未批准 pending 被隔离 | 不隐式恢复；新建计划 | 已验证 |
| D | `recover_colony_plan` | Bridge | L3 | 1 个计划 | 由 revision/done/failed/quarantine 与最新可信快照重建唯一检查点 | 有歧义则 pause，不猜测 | 已验证 |

## 批次门槛

### 批次 A：目标和个人工作优先级

- 状态必须暴露活动世界活体复制人的稳定 `KPrefabID.InstanceID`、显示名和所有可由用户调整的 ChoreGroup 当前值。
- 命令参数固定为 `duplicant_instance_id`、`expected_name`、`chore_group`、`from_priority`、`priority`。
- 仅允许 `0..5`，禁止同值写入，禁止非 `userPrioritizable` 工作组，禁止名称或实例不匹配。
- 正例必须完成“改值 -> verified -> 用 before_state 恢复 -> verified”。
- 负例必须证明实例匹配但名称错误时进入 `rejected/behavior_fail`，没有执行确认，且优先级保持原值。

### 批次 B：挖掘标记

- 坐标只接受当前世界 cell ID；去重后最多 32 个，必须全部有效、可见、非 foundation、自然实体且不含 Constructable。
- `mark` 只创建设计标记，不调用 `SimMessages.Dig` 或任何即时破坏路径。
- 每个命令记录执行前已有标记集和本命令新建集；取消不得影响表外标记。
- `DigTool.PlaceDig` 依赖 Tiles/Backwall UI 过滤状态；探针必须观测 Tiles=true、NaturalBackwall=false，命令前置条件和执行器必须再次同时校验。
- `cancel` 必须绑定 cell 与当前 `KPrefabID.InstanceID` 的精确组合，只允许 tile-only 且尚未进入执行中的 Diggable。
- `CancellableDig` 取消会先播放缩小动画；后置验证有界轮询到 `Diggable.GetDiggable(cell)==null`，不能同步判成功。
- 本机验收使用 cell `135591`：mark 创建 instance `13522`，cancel 精确绑定该实例，最终 fingerprint 恢复为执行前值；错误 fingerprint 命令仅产生 `rejected/behavior_fail`。

### 批次 C：建筑蓝图

- 初始 allowlist 只包含明确审查过的低风险 def；材料 Tag、朝向、cell、facade 全部显式。
- 放置前对所有目标执行 `IsValidBuildLocation`、`IsValidPlaceLocation`、材料配方和占用检查。
- 初始正例只考虑空 cell 上的 `Ladder`；放置入口固定为 `BuildingDef.TryPlace(...)`，永久禁止调用会生成 `BuildingComplete` 的 `BuildingDef.Build(...)`。
- 仅创建未完工 Constructable，不允许 instant build、替换已完成建筑或拆除已完成建筑。
- 路径必须同世界、连续、无重复，且整条路径先验证后执行。
- 最终 `0.6.8` / ONI `cl-740622` 验收使用零库存路径：Ladder 在 cell `138741` 选择 `WoodLog`，`materials_available=false`、`available_mass_kg=[0.0]`，创建 instance `15630`；Wire 在 cells `139386,139387` 选择 `Cinnabar`，同样为零库存，创建 instances `15998,15999`。三项均保持 `blueprint_in_progress=false` 的等待配送状态，随后按精确 instance 取消并恢复原空状态 fingerprint。库存充足 fixture 不能通过本项“缺料蓝图”验收。

### 批次 D：多周期计划和恢复

- 计划状态机固定为 `draft -> active <-> paused -> completed|aborted|failed`。
- 一次最多一个 open leaf command；只有该命令进入 verified 后才可推进检查点。
- 重启后从计划文件、done/failed/quarantine 和最新可信快照对账；任何不唯一结果都进入 paused。
- 自动恢复只能恢复控制面状态，不能自动批准、自动补偿或绕过叶子动作门槛。
- 每个 revision 带前序哈希并写入不可变文件；`current.json`、`active.json` 和根摘要原子替换。损坏尾 revision 归档到 `corrupt/` 后从最后有效链恢复。
- Bridge 控制 action 不属于 `oni.command.v2` 的游戏 action 白名单。实测计划 `plan-20260711-055131-0322d34c` 完成 revision `1 -> 6`，唯一候选命令始终未批准、未执行，最终随 abort 进入 quarantine。

## 永久禁止

- 即时挖掘或直接模拟伤害。
- 即时建造或直接生成 `BuildingComplete`。
- 拆除已完成建筑。
- 修改存档文件、生成资源或复制人、传送/杀死复制人、强制生命值变化。
- 自动批准命令，或绕过 quality、precondition、expected effect、TTL、session/save/world 绑定。

## 完成定义

单个动作只有在协议、静态安全、正例、负例、known behavior failure、恢复路径和当前 ONI build 实机后置验证都通过后，才可从 `planned` 改为 `verified`。批次完成要求该批次全部动作均为 `verified`；V2.7 完成要求 A 至 D 全部完成且永久禁止项仍不可达。
