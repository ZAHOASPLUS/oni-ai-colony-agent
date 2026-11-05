---
name: oni-player
description: Play Oxygen Not Included through the oni tools: observe the colony, decide, act, and check the result, in real time, until the stage goal is met. Use when asked to play ONI or run a colony.
---

# 玩缺氧

你就是这局游戏的玩家。没有人替你决定做什么，也没有规则替你出选项：看什么、建什么、什么时候暂停，都由你决定。

## 目标

当前阶段由任务给出。默认的两个阶段：

1. **S1 稳定存活**：连续 50 个周期不减员，氧气产出不低于消耗，食物储备不少于 3 天且不再下降，电力够用，没有持续超过 1 个周期的紧急告警。
2. **S2**：S1 之后完成一个官方使命。

用 `get_progress` 判断进度，看连续几个周期的趋势，不要只看当前这一刻。

## 开局一次性做的事

1. 读笔记 `reports/play/<run>/notes.md`。有就接着做，没有就新建。
2. 读 `knowledge/survival.md`、`knowledge/opening.md` 和 `knowledge/resources.md`。其他知识文件用到时再读。
3. `get_status` 看时间、世界边界、复制人和告警。
4. `get_map` 不带坐标看全图缩略，再带坐标看基地周围的 `terrain`、`buildings`、`gas`。
5. 写下第一版计划：接下来 3 个周期要完成什么。

## 每一轮

1. **看变化**：`wait` 或 `get_events` 的结果。把上次的 `next_since` 传回去。
2. **判断**：对照计划，这一轮最重要的一件事是什么？有紧急情况就先处理。
3. **需要时才细看**：`get_status`、`list_buildings(problems_only=true)`、`inspect`、`find`。
4. **动手**：下达操作。一轮可以有多个操作，但它们要服务于同一件事。
5. **复查**：`wait` 一小段，确认复制人真的在做，建筑真的在运转。蓝图放下不等于问题解决了。

## 节奏

游戏不等你。连续查 5 次还没动手，就该停下来下单或者 `wait`：查得再细，复制人也不会因此多干活。一次回复里可以同时发多个操作。

## 几条硬习惯

- **操作失败时先读错误**。错误里有原因和提示，照着调整。不要原样重试。
- **建好不等于能用**。建完用 `inspect` 或 `list_buildings(problems_only=true)` 确认。常见原因：没电、没管道、缺原料、没配方、没种子。
- **规划复杂布局时先 `set_speed(0)`**，想好再恢复。别让游戏空转，也别长时间暂停：暂停时什么都不会发生，平时用 2 或 3 倍速。
- **先算再建**。下单前看材料够不够，人够不够。复制人少，同时开太多工程会什么都做不完，用优先级排先后。
- **事情没进展要追查原因**。复制人闲着却有工程没做，通常是够不到（缺梯子）、缺材料，或优先级和作息的问题。`get_status` 的 `work.unreachable` 直接列出够不到的工程。

## 紧急情况

- **`duplicant_lost` 或 `critical` 告警**：立刻暂停，查原因，先救人。
- **`duplicant_low_breath`**：有人憋气到六成以下。暂停，看他在哪、去哪。10 倍速下几十秒就是好几分钟游戏时间，来不及救。
- **呼吸值下降、周围没有氧气**：这是最快的死法，放下其他事。
- **食物不足 1 天**：马上处理，优先用见效最快的办法。

## 记笔记

每完成一个阶段性目标、发现一条教训，或计划有变，就更新 `reports/play/<run>/notes.md`：

- 当前阶段目标和接下来 3 个周期的计划
- 基地布局：关键建筑的位置和 id，电路和管道走向
- 待办
- 教训：什么没用，为什么

上下文被压缩后，笔记是你唯一可靠的记忆，所以要写得让一个没看过这局的人也能接手。

重要节点用 `save_checkpoint` 存档，比如一套系统跑通时，或者要做有风险的改动之前。每个周期不必都存。

## 工具要点

- 坐标是游戏格子坐标，y 向上。`get_map` 最上面一行是 y 最大的一行，有标尺。
- `build` 的 (x, y) 是建筑原点：底行、中间一列，偶数宽度取偏左。建筑是否能放由游戏判断，直接试并读错误就行。
- 蓝图可以放进实心岩石，复制人会先挖开。
- 蓝图的 id 和建成后的 id 不同。配置建筑时用 `list_buildings` 或 `inspect` 取新 id。
- 接电线和管道时，让路径经过现有线路的一格。只是相邻不算连上。
- 能用什么建筑看 `get_buildable`，它列出了解锁状态和所需科技。想要某个建筑就直接 `set_research` 它的科技，前置科技会自动排队。
- `build_path` 也能铺成排的梯子和地砖。
- `get_status` 的 `printables_waiting` 为真时，打印舱有人选：`get_printables` 看选项，`choose_printable` 选一个，或者 `reject_printables`。不处理就不会有新复制人。
- 复制人可能重名。`get_status` 给每人一个 `id`（存档自带），`set_chore_priority`、`learn_skill`、`configure crew` 都接受这个 id；重名时只给名字会被拒绝为 `ambiguous_duplicant` 并列出 `candidate_ids`。
- 床、按摩床等个人设施用 `configure` 的 `assigned_duplicant` 指定复制人 **id 字符串**；空字符串 `""` 解除分配，允许公用的设施恢复公用。按摩床还可设 `massage_start_stress`、`massage_stop_stress`（0–100 的整数，stop ≤ start）。先用 `inspect` 核对分配和阈值，再看复制人的实际 `chore`（按摩为 `StressHeal`）及压力趋势，配置成功不等于已经开始恢复。
- `inspect` 的 `room` 是游戏对这一格的房间判定：`type` 如 `Barracks`、`Bedroom`、`MessHall`、`GreatHall`，围起来但不够格时 `type` 为 null，不在任何封闭空间时 `room` 为 null。建完卧室、餐厅后用它验收。
- 自动化：`get_buildable` 的 `logic_ports` 给出端口相对原点的偏移，`inspect` 给出已建建筑端口的实际格子、是否接上线（`connected`）和当前信号（1 绿 / 0 红）。智能电池用 `configure` 的 `smart_battery_low_pct`、`smart_battery_high_pct` 设置：电量降到下限时输出变绿，升到上限时变红，接发电机即可按需启停。
- `move_to`（复制人 id + x, y）是游戏自带的“移动到”命令：复制人走过去，在那里等到接新活。看到 `breath_pct` 下降、身处氯气/二氧化碳/高温中时，先用它把人撤回有氧的地方，再处理挖掘路线。够不着的格子会被拒绝为 `unreachable`。倒地（incapacitated）的人不能走，只能由别人自动抬到空闲的分诊床（Triage Cot），分诊床不能用 `assigned_duplicant` 手动指派。
- 间歇泉：`find` kind `geyser` 给出 `id`；`configure {"id": <id>, "study": true}` 派人去分析（需要有人会科研，重复设置不会反复切换）。分析后看 `average_kg_per_cycle`，它把喷发、间歇和休眠都平均进去了，做水或气的预算要用它，不要用 `kg_per_cycle_active`。`dormant_now` 和 `phase_cycles_left` 说明现在是否休眠、还剩几个周期。
- `get_status` 的 `colony.food_report_kcal` 是游戏的每日食物报表：`today_made`（收获、屠宰）、`today_used`（吃饭，负数）、`yesterday_net`。游戏的报表看不见做菜：成品不算 made，原料也不算 used。补充字段（today_ 和 yesterday_ 各一套）：`*_cooked` 成品 kcal，`*_cooked_inputs` 用掉的可食用原料 kcal，`*_cooked_net` = 成品 − 原料（泥土做 Mush Bar 全算新增，Mealwood 腌成 Pickled Meal 不新增），`*_net_with_cooking` 是加上做菜后的真实食物净值，**看食物盈亏就看它**。前一份日报没从头计到尾时 yesterday_* 为 null。日报在**入夜时**翻日，不是周期整点；每份报表带 `report_day`，跨 report_day 的数不要相加。
- 火箭：`configure` 的 `rocket_action`（orbit、launch、cancel_launch、land）、`rocket_restriction` 和 `crew`。

## 殖民地规则

来自 Antigravity 第 agy-r01 到 agy-r23 局的复盘（2026-11-05）：食物见底时还从 5 人扩到 12 人，19 人里死了 12 个，其中 10 个饿死，同一个检查点读了十次而不改计划。

1. **每个周期算账**。食物产出对比消耗，氧气产出对比消耗，发电对比所有建筑同时运转的用电。一个复制人每周期吃约 1000 kcal。哪个系统亏，哪个就是唯一的最高优先级，直到转为盈余。库存不是产能：补给包里的食物不能证明农场养得活人。
2. **打印舱要设门槛**。只有当库存食物够 3 天以上，且上一周期的食物产出覆盖了现有全部人口再多 1000 kcal 时，才打印新复制人。否则选食物补给包，没有就 `reject_printables`。不要打印和现有成员重名的复制人。
3. **先稳定再做使命**。只有食物够 5 天以上、氧气产出覆盖消耗、电力有余量时才推进使命。使命要的人数是最后一步，不是第一步；生存不稳时不要从目标周期倒推计划。
4. **死了人继续玩**。死亡是游戏的一部分：记下死因，在同一个殖民地里把原因修掉。不要为了撤销死亡或坏结果读旧检查点。只有工具或环境故障才读档，而且读之前先写下这次要做什么不同。
5. **用结构来调度**。用建筑优先级、固定的作息和至少维持一个周期的工作优先级。不要一会儿改一次：agy-r23 里改作息让厨师做到一半下班，给所有醒着的人关掉 `MachineOperating` 让发电机断了煤。
6. **限制在建工程**。优先级 9 的项目最多 3 个，做完再开新的。人手撑不起的蓝图和挖掘标记就取消或调低：几百个开着的工作会把饥饿的劳动力打散。
7. **缺粮先保命**。食物紧张时挖泥土、开微生物捣碎机：一根 Mush Bar 800 kcal，用 75 kg 泥土和 75 kg 水。同时扩建隔热农场；毛刺花只在 10 到 30 °C 生长。
8. **隔离气体，电力留余量**。用液封把生活区和污染气体隔开，农场做隔热，在外面干活的人配气压服。发电机用不止一种燃料，造能造的最大电池，别让一种燃料断供就让厨房停电。

## Antigravity 接入

Antigravity 用同一份 `tools/oni-tools.json`：直接会话用 `tools/antigravity_player.py`（`status`、`step`、`wait`，游标和审计写在 `--run-dir reports/play/<run>`），模型循环用 `AntigravityPlayer` 和 `antigravity_tools()`，MCP 由 `.agents/plugins/oni-player` 启动 `tools/oni_tools.py mcp`。说明在 `docs/antigravity-tool-interface-20261104.md`。
