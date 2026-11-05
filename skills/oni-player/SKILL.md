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
