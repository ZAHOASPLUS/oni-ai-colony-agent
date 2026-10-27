# B15：给电解器接电和接水（离线实现记录）

日期：2026-10-27。状态：离线实现并自测完成，尚未实盘验证。

## 问题

Phase 2 建了 6 座电解器，没有一座运转。电解器需要电，而且只能通过液体管道取水，复制人不会搬水给它。当时的动作目录只能铺 `Wire`，没有管道、泵和发电机。另外，放置契约要求目标格子完全为空，可电线和管道只有占住建筑的端口格才能连通，所以连 `Wire` 都接不上任何建筑。

## 做了什么

### Mod（Probe 与执行器）

- 建筑白名单加入 `LiquidPump`（液体泵）和 `ManualGenerator`（人力发电机）。公用设施白名单变为 `Wire`、`LiquidConduit`（液体管道），执行器原来写死 `Wire` 的地方改为读取命令参数或路径登记。
- 放置规则按建筑类型区分（`IsFreeBlueprintFootprintCell`）：
  - 普通建筑：规则不变，格子必须没有任何物体、也没有液体。
  - 公用设施：只要求自己所在的层（`def.ObjectLayer` 和 `def.TileLayer`）是空的，并且格子里没有任何未完工的蓝图；允许和已有建筑同格，也允许格子里有液体。
  - 液体泵：占地格可以有液体，但根格必须是液体，否则建出来是干的，抽不到东西。
- 候选格扫描不再扫描公用设施，因为它们只以“连线”的形式提供。预筛选也不再排除液体格。
- 新增 `blueprints.supply_links`：Probe 从已建成建筑的端口格计算出连线路径，作为诊断字段发布，不进入命令契约。
  - 电力：从第一台人力发电机的输出口出发，一条电线按由近到远的顺序依次经过所有未接电的电解器和液体泵的输入口；路径太长时，只连最近的一个。
  - 液体：每座未接水的电解器各一条管道，从最近的空闲泵出口铺到电解器的输入口，铺设方向与水流方向一致。
  - 路径搜索在新文件 `mod/Source/Probe/UtilityPathPlanner.cs` 里，是不依赖游戏的纯函数。路径最多 64 格（和执行器的上限一致），最多访问 2048 个格子。可否放置用的是和执行器相同的 `IsSafeBlueprintPlacement`；其他建筑的端口格不允许经过，避免一条线顺带接上别的建筑。
  - 每 30 秒完整重算一次。两次重算之间，每次读取都会逐格复核缓存的路径，任何一格不能放了就丢弃这条连线。已经登记过的路径 id 不会再次提供。
- 新增完工计数 `buildings.liquid_pump`；`manual_generator` 原来就有。

### 契约：公用设施可以叠放在已有物体上（两侧一致）

| | 普通建筑（不变） | 公用设施 |
|---|---|---|
| 放置前置条件 | `object_count == 0`、`empty_for_blueprint == true`、指纹等于空格指纹 | `object_count == N`（观测值）、`empty_for_blueprint` 等于观测值；N 为 0 时仍要求指纹等于空格指纹 |
| 放置预期效果 | `object_count > 0` | `object_count > N` |
| 取消前置条件 | — | 额外绑定 `object_count == M` |
| 取消预期效果 | 指纹恢复为空格指纹，`object_count == 0`，`empty_for_blueprint == true` | `object_count == M-1`；M-1 为 0 时指纹恢复为空格指纹，否则指纹必须改变；不要求格子变空 |

Python 侧在 `agent/advanced_actions.py`，C# 侧在 `ValidateBlueprintPlaceCellContract` / `ValidateBlueprintCancelCellContract`。`test_executor_model_command_contracts` 会把 Python 生成的命令交给编译出来的 C# 校验，确认双方一致。

### 候选（`agent/candidates.py`）

- 连线候选的 id 是 `power_link_<path_id>` / `liquid_link_<path_id>`，插在研究候选之后。暂停时的候选名单通常是满的，放在末尾会被截掉；而连线正是让已建好的建筑开始运转的那一步。
- 人力发电机和液体泵只在至少有一座电解器建成、并且同类建筑还一座都没有时才提供；完工计数没有观测到时一律不提供。
- 新增整条路径的取消候选 `cancel_blueprint_path_<path_id>`：一条没人开工的连线会像其他蓝图一样挡住新的放置，没有取消就会卡死。未开工的公用设施蓝图也计入“卡住”的数量。
- 研究阶梯加入 `LiquidPiping`（管道技术，解锁液体管道和液体泵），排在空气系统之后。
- 电解器闲置、又没有可提供的连线时，给出备注 `electrolyzer_idle_no_supply_link`。原来的备注 `electrolyzer_needs_power_and_liquid_pipe` 不会再出现，因为现在能提供整套供给。
- 提示词里的氧气规则改为说明完整的供给链：电解器、发电机、泵，以及两条连线。

## 自测

- `scripts/test_supply_links.py`（10 个用例）：管道叠放在端口格上的放置和取消契约；普通建筑仍然要求空格；Probe 判为不可放置的格子会被拒绝；连线候选的位置和描述；格式不对或未观测到的连线会被忽略；整条路径的取消，以及已开工时不提供取消；发电机和泵的提供条件。
- `scripts/test_executor_model_command_contracts.py` 新增一个跨语言用例：C# 接受 Python 生成的叠放放置和取消命令，拒绝 3 种篡改，也拒绝普通建筑放到被占用的格子上。
- `scripts/test_utility_path_planner.py`：编译 `tools/UtilityPathPlannerHarness`，17 项检查，覆盖绕墙、无路可走、长度和访问量上限、不跨行回绕、多个停靠点的总线不重复用格。
- Mod 编译通过（0 个错误）。产物只存放在 scratchpad，没有部署。
- 旧测试中有几处断言的正是“电解器无法供给”这种旧行为，已按新行为更新：研究阶梯的第二项、电解器的备注、`electrolyzer_supply_offered` 的取值、示例里不在白名单的建筑。

## 只能实盘确认

1. 游戏的 `IsValidPlaceLocation` 是否允许把电线或管道蓝图放在已建成建筑的端口格上。这是整套方案的前提。
2. 未完工的公用设施蓝图是否占用 `def.ObjectLayer`。即使不占用，`FindAnyBlueprint` 也会让这个格子无法通过，不会重复提供同一条连线。
3. 在公用设施层上放一个蓝图后，`object_count` 是否恰好加 1。放置的预期效果只要求大于 N；取消的预期效果要求恰好是 M-1。
4. `LiquidPiping` 是否就是解锁液体泵和液体管道的技术，有没有前置技术。如果有，执行器会确定地拒绝，拒绝原因会反馈给模型。
5. 液体泵的放置规则（2×2）与“根格必须是液体”这一条是否吻合，扫描能不能在水里找到候选格。
6. 性能：每 30 秒一次完整搜索，单条路径最多 2048 次可否放置检查。要在 Probe 的 `metrics` 里看 `blueprints` 一项的耗时。
7. 人力发电机要有复制人去踩才发电；电解器能否持续运转，还取决于工作优先级。
8. 每次停止都会恢复克隆或最新检查点；连线建好之后，要靠 B16 的检查点才能跨进程保留。
