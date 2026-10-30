# 设计：游戏工具接口与 AI 玩家（2026-10-31）

本文取代 [roadmap-20261027.md](roadmap-20261027.md) 的方向部分。旧路线图的问题清单（B1–B17）只作为历史参考。

## 0. 目标与已定决策

项目最终只有两个目标：

1. **AI 通过接口玩游戏**：读取现状、作出操作都走程序接口，任何环节都不用鼠标和键盘，包括启动后载入存档。
2. **AI 主动操作通关**：一套提示词和技能，让 AI 自己观察、计划、执行、复查。不是由规则生成选项、等触发条件，再让模型挑一个。

用户已定的决策（2026-10-31）：

| 项 | 决定 |
|---|---|
| 玩家 | 前期由 Claude 通过 MCP 工具来玩；同一套工具预留给 DeepSeek（OpenAI 风格的 function calling） |
| 通关 | 分阶段：先稳定存活 50 个周期，再完成一个官方使命（Imperative） |
| 时间 | 先实时运行；如果实时跟不上，再改为回合制 |
| 旧规则系统 | 候选生成、暂停节奏规则、审批回执链**直接移除** |

## 1. 现状评估（为什么转向）

| | 现在 | 问题 |
|---|---|---|
| 读状态 | Probe 每几秒往 `state.jsonl` 追加一份完整快照（单条可达 1 MB），Bridge 再加工成 `latest_state.json` | 推送式、体积大、为规则服务；没有按需查询，也没有“地图长什么样” |
| 操作 | 约 10 种动作；建筑白名单 10 种；研究只有 5 项的固定阶梯 | 远不够通关；要扩一项能力，就要同时改 Mod、契约、候选、测试四处 |
| 下命令 | 写文件队列 → 审批回执 → 执行 → 确认，一条命令要几十秒 | 链路长，失败面大；第一轮实盘有两次启动失败都卡在这里 |
| 决策 | Python 规则生成 ≤10 个候选，模型只能选一个 ID；暂停、恢复、重问时机都由规则决定 | 与目标 2 相反。M1 第一轮里，规则把不能研究的科技放进候选，被执行器拦截 7 次，每次游戏停住约 5 分钟 |
| 载入存档 | 需要用鼠标点“继续游戏” | 违反目标 1 |

代码规模：Python 约 3.5 万行（`agent/` + `bridge/`），另有 205 个脚本；Mod 约 2.3 万行。其中大半是回执、锚点、隔离、契约这类保障旧链路的机制。

## 2. 总体结构

```
 ┌──────────── 玩家 ────────────┐
 │ Claude Code + oni-player 技能 │      DeepSeek（Python agent 循环）
 └──────────────┬───────────────┘                 │
          MCP（stdio）                  OpenAI tools / function calling
                └──────────── oni_tools（Python）──┘
                      工具 schema 唯一来源：tools/oni-tools.json
                                   │  HTTP，仅 127.0.0.1
                    ┌──────────────┴──────────────┐
                    │ Mod：OniAiApi（游戏进程内）   │
                    │  · 请求队列 → 主线程逐帧执行   │
                    │  · 读取器（复用 Probe）        │
                    │  · 操作（复用执行器的游戏校验）│
                    │  · 事件环形缓冲               │
                    │  · 安全护栏                   │
                    └──────────────────────────────┘
```

- **Mod 是唯一的游戏接口**，只做两件事：查询，和执行一个具体操作。Mod 不做任何决策。
- **`oni_tools` 只做转发**：它从 `tools/oni-tools.json` 生成 MCP 工具和 OpenAI tools 两种格式，把调用转成 HTTP 请求，把结果原样返回。
- **决策全部交给玩家**：模型自己决定看什么、做什么、何时暂停或加速。

## 3. Mod 内的 HTTP 服务

- 用 `TcpListener` 实现一个最小的 HTTP/1.1 服务（只支持 `POST /call` + JSON），监听 `127.0.0.1` 上的随机端口，端口写在 `ai-bridge/api.json` 里。没有用 `HttpListener`：`TcpListener` 不依赖 Mono 的实现，整条路径能用 `tools/ApiServerHarness` 在游戏外验证。
- **API 模式是显式开启的**：启动脚本在拉起游戏前写 `ai-bridge/autoload.json`。Mod 读到后立即删除，超过 5 分钟的文件会被拒绝。没有这个文件时不开端口、不改任何行为，用户的普通游戏不受影响。自动存档保护、时间线保护、对话框和开场动画跳过都由这个入口开启。
- 线程模型：监听线程只负责收请求，把请求放进队列；常驻的 `ApiHost`（从 `Mod.OnLoad` 活到进程退出，主菜单也在）每帧取出最多 16 个请求，在主线程执行。排队超过 20 秒的请求会被放弃且**绝不执行**；已经开始执行的请求一定等到结果。
- 接口统一为 `POST /call`，请求体是 `{"tool": "...", "args": {...}}`，返回 `{"ok": bool, "result": ..., "error": {"code", "message", "hint"}}`。
- 鉴权：启动时生成随机令牌写入 `ai-bridge/api.json`（只有本机用户可读），请求必须带上这个令牌。
- 没有载入存档（还在主菜单）时，只允许调用生命周期类工具。

## 4. 工具清单（第一版）

坐标一律用游戏格子坐标 `(x, y)`，y 轴向上。返回内容会控制体积，大块数据都可以按区域分页取。

### 4.1 查询

| 工具 | 返回 |
|---|---|
| `get_status` | 周期、当天时刻、速度/是否暂停；每个复制人的姓名、位置、当前任务、压力、健康、呼吸、卡路里、体温；殖民地的氧气、食物（千卡与可维持天数）、电力（发电/用电/电池）；告警；当前研究与进度；在建蓝图数 |
| `get_map(x, y, w, h, layers)` | 文字网格，每格一个字符，附图例。可选的层：`terrain`（元素）、`buildings`、`utilities`（电线和各种管道）、`gas`（氧气/二氧化碳/污染氧/真空）、`liquid`、`temperature`（分档）、`designations`（挖掘/建造标记）。单次最多 64×48 格。整个小行星的缩略图用 `w, h` 超限时自动降采样 |
| `inspect(x, y)` / `inspect_building(id)` | 格子里的元素、质量、温度、气压，以及其中的建筑和物品；建筑的状态项（如“缺电”“没有管道输入”）、存储内容、可配置项 |
| `list_buildings(filter)` | 建筑实例列表：id、种类、位置、是否运转、不运转的原因 |
| `get_buildable(category?)` | 已解锁、可以建的建筑：尺寸、需要的材料和数量、耗电/产电、是否需要管道或电线、所属分类；未解锁的建筑给出它需要的科技 |
| `get_resources` | 仓库和地上可取用的物资，按元素/物品统计 |
| `get_research` | 科技树：每项科技的状态（完成/可研究/被锁）、前置条件、费用、解锁内容；当前研究和研究队列 |
| `find(kind, query, near?)` | 找最近的某种元素、物品、建筑或生物，例如 `find("element", "Algae")` |
| `get_events(since)` | 自上次以来的事件：告警出现或消失、建筑完工、研究完成、复制人状态变化、操作失败。使用环形缓冲和序号，AI 每轮先看事件 |
| `get_progress` | 阶段目标的指标（第 7 节），供 AI 自评 |

### 4.2 操作

| 工具 | 说明 |
|---|---|
| `build(def_id, x, y, rotation?, materials?, priority?)` | 放任意已解锁建筑的蓝图；`materials` 不填时用游戏默认的材料 |
| `build_path(def_id, cells, materials?)` | 电线、液管、气管、运输轨道等需要连成路径的建筑 |
| `dig(x, y, w, h, priority?)` | 标记挖掘一片矩形区域 |
| `deconstruct(target)` | 拆除：建筑 id 或一片矩形区域 |
| `cancel(x, y, w, h)` | 取消区域内的挖掘、建造和拆除标记 |
| `set_priority(target, level)` | 设置建筑或标记的优先级 1–9 |
| `set_chore_priority(dupe, chore_group, level)` | 设置复制人的工作优先级 |
| `set_research(tech)` / `queue_research([...])` | 研究任意科技；前置条件不满足时，自动把前置科技排进队列，或返回错误（由参数选择哪种） |
| `set_schedule(schedule, blocks)` | 作息表 |
| `configure(building_id, settings)` | 建筑设置，例如储物箱筛选、阈值。第一版只做常用的几种 |
| `set_speed(speed)` | 速度 0（暂停）/1/2/3 |
| `save_checkpoint(note)` | 存一个检查点（复用现有的检查点链） |

**合法性只由游戏端判断**：用游戏自己的放置检查、材料检查、科技检查，失败时返回能直接照着改的原因，例如 `{"code": "material_insufficient", "message": "需要 400kg 金属矿，可用 120kg", "hint": "可用材料: Sandstone 2.1t, Copper Ore 120kg"}`。不在 Python 侧预先过滤。

### 4.3 生命周期（不需要已载入存档）

| 工具 | 说明 |
|---|---|
| `load_save(path)` | 在主菜单直接调用 `LoadScreen.DoLoad`。现有的 `ValidationCheckpointController` 已经这样做过 |
| `get_lifecycle` | 当前处在主菜单、载入中还是游戏中，以及存档名 |

另外支持**启动时自动载入**：Mod 在主菜单出现时读取 `ai-bridge/autoload.json`，有就载入其中的存档，然后删除这个文件，整个过程不用鼠标。对话框（Mod 变化提示、开场动画）由 Mod 在启动时自动关闭或跳过。

## 5. 安全护栏（保留并精简）

只保留下面这些硬规则，它们都**不参与决策**：

1. **只在测试克隆上开放写操作**：存档名必须以 `oni-` 开头，并且通过 `clone_launch_guard` 的校验。用户原来的存档只读，读也只在明确允许时进行。
2. **自动存档保护**（`AutosaveGuard`）：载入时强制把自动存档间隔设为 0，防止轮换覆盖原存档。
3. **禁止的操作**：拆除打印舱；删除或覆盖任何存档；修改 Mod 或游戏设置。
4. **检查点链**：只能存新的检查点，不能覆盖已有的（`checkpoint_chain`）。
5. **审计日志**：每次调用都追加一行 JSONL（工具、参数、结果、游戏周期），用于事后复盘，不做实时审批。

## 6. AI 玩家技能 `oni-player`

技能放在仓库的 `skills/oni-player/`，由三部分组成：

1. **工作方法**（`SKILL.md`）：
   - 每一轮：`get_events` → 看情况决定要不要 `get_status`/`get_map` → 对照计划决定这一轮做什么 → 执行操作 → 让游戏运行一段 → 复查结果。
   - 操作失败时，先读错误原因再调整，不要重试同一个操作。
   - 遇到复杂布局，先暂停（`set_speed(0)`），规划完再恢复。
   - 每 N 轮，或者有重要事件时，更新笔记。
2. **游戏知识**（`knowledge/`）：开局三十个周期的打法、生存优先级（氧气 > 食物 > 卫生 > 电力 > 温度）、科技路线、常见布局（厕所与洗手池、卧室、农场、电解制氧、氢气发电）、常见陷阱（二氧化碳下沉、污染水、温度）。按需读取，不全部塞进提示词。
3. **笔记文件**（每次运行一份，`reports/play/<run>/notes.md`）：当前阶段目标、计划、待办、教训。跨会话、跨上下文压缩时用它延续，配合检查点存档。

DeepSeek 用的是**同一份技能**：Python 循环把 `SKILL.md` 作为系统提示词，知识文件作为 `read_knowledge(topic)` 工具，笔记作为 `read_notes`/`write_notes` 工具。

## 7. 阶段目标与评估

| 阶段 | 达成标准（`get_progress` 直接给出） |
|---|---|
| S1 稳定存活 | 连续 50 个周期：复制人不减员；氧气产出 ≥ 消耗（按周期平均）；食物储备 ≥ 3 天且不在下降；电力满足需求；没有持续超过 1 个周期的紧急告警 |
| S2 一个使命 | 游戏内一个官方 Imperative 完成（具体哪一个在 S1 完成后再选） |

每次运行都记录：周期数、每个周期的关键指标、调用次数和失败率、模型 token 用量。用于对比提示词和技能的不同版本。

## 8. 代码去留

| 保留并迁移 | 移除 |
|---|---|
| Mod：`ProbeStateReader`、`SurvivalTelemetryReader`、各 `FunctionalBuilding*Reader` 中读游戏状态的部分（改成按需查询）；`ActionExecutor` 中的游戏端放置和校验逻辑；`AutosaveGuard`；`ValidationCheckpointController` 的存档和载入部分；`LifecyclePatches` | Mod：`ExecutionClaimStore`、`OutcomeLatch`、`PreparedConfirmationRecovery`、`CommandFieldContracts`、`CanonicalHashContract`、`StartupBinding` 的锚点链、文件命令队列、整份快照的推送 |
| Python：`checkpoint_chain`、`clone_launch_guard`、`llm_planner` 的模型提供方配置和格式兼容部分（给 DeepSeek 循环用） | Python：`candidates`、`model_loop`、`controller_loop`、`rules`、`decision`、`strategy`、`colony_plan`、`plan_coordinator`、`command_queue`、`command_custody`、`approval_receipt`、`autonomous_authorization`、`action_outcomes`、`runtime_confirmation`、`control_packet`、`field_contracts`、`state_quality`、`decision_frame_archive`、`bridge/oni_bridge.py`，以及对应的脚本和测试 |

移除在新分支上按提交分步进行（每一步都能回看）；在新接口能跑通第 9 节的 P2 之后再删，避免搬迁期间丢了可复用的代码。

## 9. 分步计划

| 步骤 | 内容 | 完成标准 |
|---|---|---|
| P1（已完成） | Mod HTTP 服务 + 查询工具（`get_status`、`get_map`、`inspect`、`get_buildable`、`get_research`、`get_events`）+ `oni_tools` MCP 包装 + 自动载入 | 启动游戏后不碰鼠标就进入克隆存档，Claude 能通过 MCP 看到殖民地和地图 |
| P2（已完成） | 操作工具（`build`、`build_path`、`dig`、`deconstruct`、`cancel`、`set_research`、`set_priority`、`set_speed`、`save_checkpoint`）+ 游戏端错误原因 + 审计日志 | Claude 手动调用工具，在克隆存档上建好厕所、挖出一个房间、开始研究 |
| P3 | `oni-player` 技能 v1，Claude 实时玩 10 个周期 | 不需要人插手，10 个周期内没有减员；整理出失败原因清单 |
| P4 | 移除旧系统 | 第 8 节右栏全部删除，剩下的测试通过 |
| P5 | DeepSeek 循环（同一份 schema 和技能） | DeepSeek 实时玩 10 个周期 |
| P6 | 迭代到 S1；再选使命做 S2 | 第 7 节标准 |

### 实机验证结果（P1 + P2，2026-10-31）

两轮实机运行，证据在 `reports/api/p1-r02/`、`p1-r03/`，都在克隆 `oni-m1-terra-r01` 上进行。

| 完成标准 | 结果 |
|---|---|
| P1：不碰鼠标进入克隆存档 | 通过：启动后约 26 秒到 `in_game`，两轮都成功，开场动画和模组差异对话框都没有拦住流程 |
| P1：能看到殖民地和地图 | 通过：8 个读取工具都有真实数据，单次约 15–30 毫秒；通过 MCP 通道实测过 |
| P2：挖出房间、建好厕所、开始研究 | 通过：6×3 房间挖完，`Outhouse` 建成，研究设为 `FarmingTech` |

其他已确认的行为：
- 复制人收到挖掘和建造后立刻响应，优先级生效；夜里睡觉时蓝图会等到天亮。
- `build_path` 的连接是真实的：发电机和电池接在同一条线上，`circuits` 为 1，电池开始充电，复制人自己去手摇发电。
- 取消、打印舱保护、材料不足、未解锁、路径不相邻等拒绝路径都返回了能照着改的原因。
- `save_checkpoint` 写出 `ckpt-002`，游戏继续运行，载入指针不变；`SaveLoader.Save` 也写了 `.png`，修正了 D4 里“不写预览图”的说法。
- 两轮结束后，已有的 350 个存档和预览文件全部未变，只新增 `ckpt-002` 的两个文件。

这一轮暂时用 `tools/oni_tools.py call` 和 MCP 脚本调用工具。Claude 要在新会话里才能直接看到 `.mcp.json` 注册的 `oni` 工具。

已知的待办：
- `save_checkpoint` 还不发布检查点清单，`api_session.py start` 也还不能从检查点启动。这要在 P3 之前补上，否则每次启动都回到克隆的起点。
- `building_completed` 事件里混有地图自带的建筑（`TilePOI` 等），它们在开局后陆续生成，不是复制人建的。
- 研究站要 400 kg 金属，所以这一轮研究只设置了目标，没有实际产生进度。
- `build` 不支持替换已有的地砖；`build_path` 不处理跨管线的桥接。
- 第 4 节里的 `list_buildings`、`get_resources`、`find`、`get_progress`、`set_chore_priority`、`set_schedule`、`configure` 不在 P1/P2 的完成标准内，还没有实现。

## 10. 风险与未知

1. **HTTP 服务**：已改用 `TcpListener`，这条风险取消。
2. **地图表示**：一个字符表示一格，信息是否足够，token 是否太多（64×48 ≈ 3k 字符）。P1 拿 Claude 实际试，必要时改成分层加摘要的形式。
3. **实时节奏**：Claude 一轮约 10–30 秒，一个游戏周期在 1 倍速下是 600 秒，理论上够用。如果跟不上，就改为回合制（工具不变，只是技能里约定先暂停再操作）。
4. **长时间运行**：Claude Code 会话会进行上下文压缩，靠笔记文件和检查点延续。
5. **游戏版本**：读取器绑定了当前版本（构建 744825），游戏更新后可能需要调整。
6. **对话框和开场动画**：第一轮实盘出现过开场动画，Mod 要能在载入流程中自动跳过。
