# ONI 项目目标梳理与代码审查报告

审查日期：2026-09-26。审查对象：`C:\Users\user\Documents\ONI` 当前源码（最后改动 2026-09-16），以及已安装的 Mod（哈希 `336e42f9…`，与本地构建一致）。

审查方式：
- 主审阅读文档、会话记录和证据。
- 8 个并行审查代理分模块只读审查：C# 执行器、C# Probe 核心、C# 遥测读取器、Bridge、命令托管、决策层、策略/LLM、实机环境脚本。
- 所有 P0 和关键 P1 都经主审用源码或复现脚本二次核实，在下表"核实"列标 ✔；只由代理复现的标"代理"。

全程未启动游戏、Bridge 或任何实机脚本，也未改动工作区或 Klei 目录中的文件。本报告是唯一新增的文件，结束时已按修改时间核对。

测试基线：离线单元测试 18 个模块、196 项全部通过（主审 14 个模块 121 项，代理 4 个模块 75 项）。这只说明离线逻辑自洽，不代表实机可用。

---

## 1. 项目目标

### 1.1 目标的三个层次
| 层次 | 内容 | 来源 |
|---|---|---|
| 原始目标 | 接入外部 LLM API，让它实际去玩《缺氧》 | 用户原话"接入外部api使其玩缺氧"（`reports/goal-alignment-audit-20260913-01a09159.md`） |
| 最低验收 | ① 模型真实生成成功；② 模型基于当前观测，从受限动作中做出选择，并且这个选择真正决定了执行什么；③ 经审批进入真实游戏；④ 后续观测确认效果，并反馈给下一次决策；⑤ 在有限任务中重复，取得可测量的进展 | 同上 |
| 终局契约 | 完成绑定版本/DLC/场景下所有适用的官方 Imperatives；六阶段（早期生存→稳定→扩张→工业→航天→可持续）可执行；有限工业、资源窗口验收；学习闭环；安全收尾 | `docs/controller-finite-completion-contract-m32-20260910.md` |

### 1.2 实际状态（截至 2026-09-16 的最后一轮）
- 约 26 次实机尝试（09-13～09-16），至今没有一次"模型驱动"的实机闭环。最基础的实机里程碑"暂停往返"（F0→C1→F1→C2→F2，不涉及模型）也还没通过：run17 只有 C1 真实执行，没拿到可信 F1。
- LLM 只被实现为"对已有事件 ID 排序"：默认 `enable_llm=False`，提示词禁止产生动作。排序结果没有任何运行时路径进入命令队列（见 M1）。TokenHarbor 实测一次请求 30 s 超时后回退到规则。
- 可执行动作：暂停、研究、单个复制人优先级、挖掘标记/取消、Ladder/Wire 蓝图。不能建造生命支持建筑，也无法形成生存闭环。
- 代码分布（行数）：

  | 类别 | 行数 |
  |---|---|
  | 真正"玩游戏"的逻辑（strategy/rules/planner/plan/controller/actions） | 约 1.0 万 |
  | Mod（Probe + 执行器） | 1.87 万 |
  | 控制平面 | 约 1.2 万 |
  | 环境与证据脚本 | 约 4.3 万 |
  | 验证与测试 | 约 7.7 万 |
  | 文档（89 份中 47 份是恢复/交接/评审类过程日志） | 1.3 万 |

### 1.3 目标偏移诊断
1. 验收标准从"模型玩起来"滑向"证据与契约完备"。过去两周的主要产出是 13 个只读遥测读取器、印章（seal）报告和恢复文档。其中 12 个功能建筑载荷没有任何决策代码使用，却占快照约 96% 的节点和约 91% 的采样时间（见 L13）。
2. 安全机制本身复杂到正常流程走不完：自动存档、帧抖动、托管冲突等问题让正式流程失败，操作者只能用临时脚本绕过（run17 从未 finalize，run18 用临时脚本恢复）。真实的用户数据损害恰恰出在这些绕过路径和游戏自身行为上（见 P0-1）。
3. 大量精力花在管理 AI 开发代理本身：AGENTS.md 48 行里大部分在讲 Codex 的网络、模型路由和上下文压缩。

### 1.4 建议重新确立的目标层级
- **M1（下一个、也是唯一的当前目标）：一次完整的模型驱动实机闭环。**
  - 每个 tick 从可信快照生成不超过 5 个已编目、条件完整的候选动作。
  - 模型只返回一个 `candidate_id`，使用结构化输出，异步调用，设总截止时间。
  - 生成单叶子命令，走现有审批链。
  - Mod 执行后，由后续快照确认效果，并把结果写进下一轮提示。
  - 首选动作：`set_duplicant_chore_priority`（需先修 L12 的中文名问题），或不超过 4 格的 `mark_dig_cells`。
- **M2：有限任务取得可测量进展**，例如挖通一段通道，或建成一段真正连通的电线（需先修 G1、M2、M3）。
- **M3：生存闭环**（氧气/食物/电力），以收支速率（ReportManager 已有 `OxygenCreated`/`CaloriesCreated`/`EnergyCreated`）为证据。
- **M32 终局保持为长期方向**，但在 M1 通过前冻结新增遥测、证据工具和多步认证。

---

## 2. 需要你立即处理的事项（涉及你的真实数据，我没有动，需要你确认）

1. **原始存档已丢失一份，丢失机制仍然有效（P0-1）。**
   - 丢失的是 `cloud_save_files\76561190000000000\娴雅的太空监牢\auto_save\娴雅的太空监牢 Cycle 6.sav`（2024-12-01，sha256 `2471f42f…`），磁盘上已找不到。
   - 目前最新的存档是测试克隆写进同一目录的 `user-nonnoop-loader-20260913-17 Cycle 8.sav`。**在清理之前，不要在游戏主菜单点"继续游戏"。**
   - 建议把克隆的 `Cycle 7/8` 两组 `.sav`/`.png` 移到工作区证据目录。这是移动不是删除，可以还原。
   - 如果还想找回 Cycle 6，只能看 Steam 云存档或 Windows 文件历史。游戏用 `File.Delete` 删除，不经过回收站。
2. **Mod 配置**：21 个 Mod 在 9 月 13 日按你的指示（"注意关闭所有mod"）被禁用，至今保持禁用。原配置备份在 `reports/controller-plan-sequence-20260913/user-live-profile-preflight-20260913/mods.json.before-newgame`；更早的一份是 `mods\mods.json.pre-oni-ai-restart-20260830-1633.bak`。另外，`kplayerprefs.yaml` 里的 `SaveFilenameKey/` 目前指向一个测试克隆。
3. **根目录的 `.tmp_*.py/.ps1` 和 `scripts/restore-test-save.ps1` 有破坏性**（见 S3、S4），建议移出工作区或在第一行加硬性拒绝。

---

## 3. 结论总览

| 严重度 | 数量 | 说明 |
|---|---|---|
| P0 | 2 | 用户存档实际丢失；"结果未知即锁存"这条核心安全不变量可被正常流程解除 |
| P1 | 29 | 其中约 13 条直接导致实机"跑不通或跑不久"，多数只需少量改动 |
| P2/P3 | 约 35 | 边界情况、性能、可维护性 |

值得肯定的部分（第 9 节）：
- 至多一次执行的设计扎实。各审查员逐点追踪了崩溃、重启、读档路径，没有发现重复执行。
- 契约整体坚持 fail-closed。
- 非浮点值的规范化哈希在 Python 与 C# 之间逐字节一致。
- 生存、告警、报表三个读取器的单位全部正确。

---

## 4. P0

| # | 问题 | 位置 | 失败场景与证据 | 核实 |
|---|---|---|---|---|
| P0-1 | 测试克隆与原档放在同一殖民地目录，游戏的自动存档轮换删除了原始存档 | `scripts/create-named-test-clone.ps1:273,296-297`；`scripts/manage_live_validation_environment.py:2567, 4702-4751`；游戏 `_audit_full/SaveLoader.cs:156, 789-825` | 自动存档时，游戏只保留"当前存档目录\auto_save"中与本殖民地 GUID 相同的最新 10 个存档。克隆是字节拷贝，GUID 相同，它在 09-13 23:43/23:46 两次自动存档后，原档 Cycle 6 被删。项目自己的边界记录显示这份文件在 run17 pre-boundary（22:49:44）之前一直存在，之后全部缺失，而比较器把丢失当成了新基线。继续玩克隆会接着删除 Cycle 7、8…… | ✔ |
| P0-2 | 结果未知锁存会在检查点存档或重载后失效 | `agent/command_queue.py:233-236, 646-667`；`mod/Source/Probe/ProbeSession.cs:343`；`mod/Source/Commands/ActionExecutor.cs:75` | Python 门禁在 `command.save_id_hash != latest.save_id_hash` 时忽略 outcome_unknown，而 `save_id_hash` 是内容哈希，每次成功的手动或检查点存档都会变。C# `BeginSession()` 每次读档执行 `mutationSafetyLatch = false`，也不读 `failed/`。G0 流程本身就包含"存档→重载"，锁存正好在这里失效。另外，没有 classification 字段的 failed 记录和原始 executing claim 也不会触发锁存。 | ✔ |

---

## 5. P1

### 5.1 导致实机"跑不通/跑不久"的问题

| # | 问题 | 位置 | 失败场景与证据 | 核实 |
|---|---|---|---|---|
| L1 | 帧 ID 每轮都变，待批命令每轮作废重建 | `agent/decision.py:511, 988-993` | 帧摘要包含 `health.snapshot_age_seconds`。没有新快照时，每轮 poll 帧 ID 仍会变，pending 被判 `pending_source_frame_stale` 并以新 ID 重建（复现：rejected 依次 0→1→2→3）。人工批准只剩约一个轮询周期；`commands/rejected` 每条建议每小时新增约 1,400–1,800 个文件 | ✔ |
| L2 | 官方启动脚本让策略证据窗口每轮清零 | `bridge/oni_bridge.py:580` 对比 `:2848-2850`；`scripts/run-bridge.ps1:16`；`agent/strategy.py:52-54, 749-756` | `process()` 用默认 `STRATEGY_GOAL`，`write_outputs` 用 `--goal`，而 run-bridge.ps1 的默认文本与之不同。同一快照得到两种哈希，触发 `strategy_duplicate_conflict` 和 `_reset_window()`，复现中已累积的 7 个样本归零 | ✔ |
| L3 | 一次瞬时打开失败，整个游戏进程永久失去自动控制 | `mod/Source/Probe/ProbeWriter.cs:310-343, 236-248`（`safetyFault` 只会置 1）；`ProbeRunner.cs:49, 97-100` | 写线程每 250 ms 无条件打开日志，任何异常都记为进程级永久故障。撞上 Bridge 轮转或 `shutil.copy2`/`Copy-Item` 复制活动日志时的共享冲突即可触发（复现 20 s 内 48k 次 WinError 32）。估计连续 8 小时运行约有 25% 概率触发 | ✔ |
| L4 | Bridge 主循环没有异常边界，run-bridge.ps1 失败时仍返回 0 | `bridge/oni_bridge.py:3360-3414`；`scripts/run-bridge.ps1:84,112` | run18 的根因（`CommandCustodyConflict`）至今未修。lifecycle unknown 时每次重启第一轮就抛异常，形成崩溃循环 | 代理 |
| L5 | 任意一条 focus_note/noop 成功后，托管层永久瘫痪 | `agent/command_custody.py:916-927` 对比 `ActionExecutor.cs:2517-2521` | C# 对非变更命令写 `done/status=verified`，Python 只接受 `confirmed`，之后所有托管操作都抛 `CommandCustodyConflict`，Bridge 每代退出 | ✔ |
| L6 | 日志轮转后重启 Bridge，会话再也无法恢复信任 | `bridge/oni_bridge.py:2306-2333, 225-242, 415-423` | 启动回放只读活动的 `state.jsonl`，而 `session_started` 已被轮转到 `.1`。真实证据：`user-live-developed-20260913-08/final-cleanup/bridge-before/` | 代理 |
| L7 | 会话归档无界增长且每轮全量扫描，12–18 分钟后失信 | `bridge/oni_bridge.py:1894-1991, 2154-2262`；`agent/action_outcomes.py:510-562`；`agent/controller_bridge.py:32, 268-273` | 实测归档 10.45 分钟 376.6 MB（约 2.2 GB/h）。有未决命令时每轮扫描 4 遍，377 MB 时每轮 8.4 s。`observe_effect` 有 64 MiB 上限，约 2 分钟后就超出，结果恒为 UNKNOWN | 代理（实测） |
| L8 | 漏一个快照，整个事件周期长期阻断紧急暂停 | `agent/incidents.py:185-193`；`bridge/oni_bridge.py:522-535` | Bridge 遇到不可信快照直接 return，不更新事件；tracker 又不检查 `snapshot_seq` 连续性，一直报 `sample_sequence_gap`。复现：漏掉 1 个快照后再过 60 个样本，窒息暂停仍被阻断 | ✔ |
| L9 | 同一个仲裁 reducer 被喂了不同的输入集合 | `agent/decision.py:667-670`（全部事件）对比 `agent/rules.py:1524-1527`、`agent/command_queue.py:883-891`（只取 open） | 低氧处于 pending/cooldown 时，窒息、饥饿、失能的暂停在审批环节被拒（`survival_command_selection_mismatch`），延迟 30–60 s | ✔ |
| L10 | Python 严格清点与不持锁的 C# 执行器冲突；观察器无锁读取阻塞 rename | `agent/command_custody.py:886-938`；`ExecutionClaimStore.cs:53-86, 198-217`；`bridge/oni_bridge.py:2800, 2979`；`scripts/commit_command_approval.py:230-234, 679` | C# 的正常中间态（approved 内 status=executing、`*.tmp-<guid>`、复制加删除）会让 Python 扫描抛异常（模拟约 960 次全部失败）。不带 FILE_SHARE_DELETE 的读句柄让批准或执行器的 rename 失败（200 次批准中 7 次失败，5 次留下 approval-recovery 卡死）。L21 只修了测试观察器 | 代理 |
| L11 | set_research 的效果期望与游戏语义不符，必然被判为 outcome_unknown | `ActionExecutor.cs:1117-1127, 1545-1560, 1408-1421`；游戏 `_audit_full/Research.cs:198-227` | 游戏会把目标及其所有未完成前置加入队列，并把最低 tier 的那项设为当前研究，所以"current == 目标"几乎总是不成立。结果是玩家的研究队列被替换，会话被锁存并暂停 | ✔ |
| L12 | 批准脚本损坏中文字符，中文名复制人的优先级命令无法批准 | `agent/approval_receipt.py:389`；`scripts/approve-command.ps1:167-172, 208-213` | 用户环境 `PYTHONUTF8=1`，而 PowerShell 5.1 的 OutputEncoding 是 gb2312，回执 JSON 被破坏，validate 失败。G0 用的正是这个动作 | 代理 |
| L13 | 快照体积和主线程开销随全部建筑实例线性增长，中后期 AI 完全失明 | `ProbeStateReader.cs:168-219`；各 `FunctionalBuilding*Reader`；`ProbeWriter.cs:33, 359-369, 541-546` | 第 6 周期、4 名复制人时已有 1221 个实例（71–77% 是世界生成的 POI 墙和道具），单快照 3–4.5 MB，主线程 p99 248 ms、最大 615 ms。约 1,856 个实例时超过 4 MiB 证据上限；约 8,400 个时超过 100 万节点，整份快照（包括窒息告警）被拒，并触发粘性故障。12 个功能建筑载荷没有任何决策代码使用 | 两个代理分别实测 |

### 5.2 游戏语义错误

| # | 问题 | 位置 | 失败场景与证据 | 核实 |
|---|---|---|---|---|
| G1 | Wire 蓝图从不登记格与格之间的连接 | `ActionExecutor.cs:1831-1843`（`mod/Source` 中没有任何 `AddConnection`）；游戏 `_audit_full/WireBuildTool.cs:17-38` | 建成后是一串互不连通的单格电网，后置条件不检查连通性，命令照常被确认成功 | ✔ |
| G2 | 可达性遥测：读档后首个快照全部为 false；实际测的是拆除可达性 | `FunctionalBuildingReachabilityTelemetryReader.cs:105-116` | 12 个会话的首快照都是 1221/1221 为 false，却标 complete。`GetComponent<Workable>()` 取到的是 Deconstructable，`IsAllReachable` 的语义是"任一格可达" | 代理（实机数据） |
| G3 | 告警只覆盖活动世界（`checkChildWorlds:false`），却标为 complete | `CanonicalAlertReader.cs:37-39`；`agent/rules.py:351-366` | 第二颗小行星或停靠火箭舱里的复制人窒息时，`alerts=[]`，所有告警信号被判为 inactive，暂停不会触发 | 代理 |

### 5.3 LLM 与规划链路

| # | 问题 | 位置 | 失败场景与证据 | 核实 |
|---|---|---|---|---|
| M1 | 模型输出没有任何运行时路径进入执行 | `bridge/oni_bridge.py:2825-2843, 2957-2971`；`agent/llm_planner.py:185-257`；`agent/colony_plan.py:241-249` | 默认路径同步的是规则 advice。唯一使用排序的 planned-owner 路径必然抛异常（`set_paused` 不在目录中，或 `no trusted leaves`）；它的进程门禁还要求每个 tick 都没有游戏和 Bridge 进程在运行，结果 Bridge 退出。相关测试把这些环节都 mock 掉了 | 代理，主审核对默认路径 |
| M2 | 运算符拼写 `ne`/`neq` 不一致；条件数上限过低 | `agent/controller_loop.py:52, 368, 409, 834`；`agent/field_contracts.py:129-131` | 控制器拒绝所有 Ladder/Wire 放置叶子；条件上限 32 使 Ladder 最多 2 格、Wire 1 格、挖掘 4 格 | 代理 |
| M3 | 多步计划要求上一步效果持续成立 | `agent/colony_plan.py:543-546, 1070-1075, 1296-1303` | 蓝图建成、挖掘完成后标记消失，计划报 `checkpoint_state_mismatch` 并永久卡死。所有高级动作又都要求游戏暂停，暂停时复制人不干活 | 代理 |
| M4 | 模型结果只存活一个 tick；失败后同一组事件永不重试 | `agent/llm_planner.py:95-148, 175-181` | 调用前就写入指纹，一次超时就永久压制重试；成功排序在下一 tick 被覆写回规则顺序 | 代理 |
| M5 | 模型调用同步阻塞在"信任冻结"与"生成命令"之间；超时语义有误 | `bridge/oni_bridge.py:2796-2843`；`agent/llm_planner.py:25-26, 543-598` | `urlopen(timeout=30)` 是单次 socket 操作超时，不是总截止时间，调用可能远超 30 s；阻塞期间快照已过期，仍会基于它生成命令。请求没有结构化输出；`temperature: 0` 在部分推理模型上会返回 400；`max_calls_per_hour` 配置没有任何代码读取 | 代理 |
| M6 | 安全：secrets 目录解析可能把 Key 发到错误主机 | `agent/llm_planner.py:449-514` | 文本文件里任何 `http(s)://` 行或 `BASE_URL=` 行都会无条件覆盖环境变量中的 base_url（最后一个生效）；30 字符以上、像 token 的行在没设 Key 时会被当成 Key；接受 `http://` | ✔ |
| M7 | 生产环境中策略阶段永远停在 bootstrap | `agent/strategy.py:2169-2175, 2240-2259`；Bridge 不传 `trusted_proof_verifier` | Probe 输出的各项速率均为 null，氧气和食物目标恒为 unknown | 代理 |
| M8 | 信任要求"只有一个有人世界，且就是镜头所在世界" | `agent/strategy.py:1287-1295`；`agent/state_quality.py:383-392` | 复制人进了火箭舱或第二颗星球，甚至只是切换镜头，整条链路都会失信，与 M32 目标矛盾 | 代理 |
| M9 | 克隆启动守卫没有把存档路径绑定到 manifest | `agent/clone_launch_guard.py:86-97` | 原档的任何一份内容相同的拷贝都会被当作克隆放行（潜在问题，目前还没有启动器调用它） | 代理 |

### 5.4 安全机制与环境

| # | 问题 | 位置 | 失败场景与证据 | 核实 |
|---|---|---|---|---|
| S1 | 证书一致性只修了原始用例，审批路径不重新仲裁 | `agent/rules.py:837-894, 1044-1056`；`agent/approval_receipt.py:225-236` | 把 witness 的 status 改为 resolved/pending 或直接删除，两个校验器都接受，而真实仲裁器判 blocked。候选准入阶段有复核，所以不是 P0 | ✔ |
| S2 | 环境基线每轮漂移；Mod 隔离从未生效；测试和缺陷一样错 | `scripts/manage_live_validation_environment.py:2631-2645`；`scripts/configure_live_validation_runtime.py:4239-4296`；`scripts/verify_live_validation_runtime_config.py:1273, 1471`；游戏 `_audit_full/KMod/Mod.cs:220-230` | 每轮都以当时状态作为要恢复的基线。游戏按 `enabledForDlc` 决定加载哪些 Mod，只改旧 `enabled` 字段无效，所以"只启用 Probe"的证据断言是假的 | ✔ |
| S3 | 根目录临时脚本有破坏性 | `.tmp_final_cleanup.py`、`.tmp_restore_probe.py`、`.tmp_retry_bridge*.py`、`.tmp_restart_prep10.py` 等 | 直接 `rmtree` 实时的 ai-bridge 和 Probe 目录；用未校验的旧备份覆盖用户配置；路径依赖当前目录；在证据里写死 `'processes':{'oni':0,'bridge':0}`，实际没检查；重跑会删掉唯一一份"恢复前"快照 | ✔ |
| S4 | `restore-test-save.ps1` 可以把任意 baseline 覆盖到任何存档 | `scripts/restore-test-save.ps1:23-42, 52-58` | 只检查父目录位于存档根下，不检查目标是否为克隆。`docs/dev-notes.md:109` 仍把它列为可用工具 | 代理 |

---

## 6. P2/P3（精简）

**Probe / Mod**
- `rotate_log` 不是事务性的：最后一步 rename 失败时备份已经移位，每失败一次丢一代（`oni_bridge.py:3251-3265`）。
- 写入中途失败留下的半行会吞掉下一条记录，例如一条 ACK（`ProbeWriter.cs:326-343`）。
- 记录被丢弃时 `WriteEvent` 仍返回正 seq（`ProbeWriter.cs:107-120`）。
- 缓存刷新期限用的是墙钟（`ProbeStateReader.cs:38` 等）。
- StartupBinding 接受残留的旧绑定文件：不检查新鲜度、不一次性消费（`StartupBinding.cs:186-221, 419-485`；9 月 13 日的两个进程都读到了 9 月 9 日的绑定文件）。
- 优先级遥测没有反映被特质禁用的任务组，优先级动作可能"成功"却没有效果（`ProbeStateReader.cs:706-710`）。
- 执行器：
  - 暂停是引用计数的，unpause 会抢走模态窗口的暂停引用（`ActionExecutor.cs:1517-1543, 2654-2678`）。
  - done/confirming 检查存在 TOCTOU（`:2362-2413`）。
  - 遇到共享冲突不重试，已成功的变更被判为未知（`ExecutionClaimStore.cs:198-217`）。
  - 见证轮询每帧运行不节流，出异常时每帧重新暂停（`:2352-2421`）。
  - 终态记录内嵌完整前后状态（单条 11 MB），`HasProcessedId` 在主线程全量解析历史（单文件 192–319 ms、分配 177 MB）。
  - Harmony 回调没有 catch（`LifecyclePatches.cs:136-153`）。
  - `ParseObjectStrict` 实际接受 `010`/`0x10`/单引号等非标准 JSON。
  - `FinishPendingFailure` 是死代码，而 `docs/survival-action-gap-audit.md` 仍把它列为 P0。
- 遥测读取器：
  - `Workable.GetOffsets()` 会注册分区监听，并非只读（`...ReachabilityTelemetryReader.cs:111`）。
  - `occupied_cells` 永远是 null，API 用错了对象（`...OccupancyTelemetryReader.cs:215-255`）。
  - 电网读取器只读 `Wire`，`network_id` 跨快照不稳定。
  - 次级管道端口被忽略。
  - 管道"流量"其实是单格瞬时量除以墙钟时间（`utility_flow.py` 在生产中也是死代码）。
  - 电池遥测在 0.7.0 的实机证据中 100% 为 error。
  - 13 份复制代码已经分叉。
- 卫生目标只统计 Outhouse，只用抽水马桶的殖民地会被判为紧急状态（`strategy.py:2396-2413`）。

**Bridge / 控制平面**
- `session_ended` 标志没有锁存（`oni_bridge.py:509`）。
- 重启成本与数据量成正比：142 MiB 日志时峰值 477 MiB。
- `atomic_write_text` 在 replace 前不 fsync。
- 无效 JSON 被计数两次。
- 规范化哈希在浮点上跨语言不一致：`-0.0`，以及 1 ULP 的差异（`canonical_hash.py:63-69` 对比 `CanonicalHashContract.cs:69-75`）。
- TTL 以"完成时间"而非"认领时间"为准，临近过期批准的成功变更永远无法确认（`action_outcomes.py:822-826`）。
- 托管操作在锁内全量扫描 9 个阶段目录：3000 个文件时单次 10.6 s，超过 5 s 的锁超时。
- `verify_runtime.py` 的 prepare 写入器绕过托管和门禁。
- 审批回执只做绑定，没有认证：同账户下任何进程都能写 `approved/`。文档已承认这一点，但 pause-roundtrip 就是自动批准的。
- 各类恢复残留（approval-recovery 目录、私有锁）会卡死托管层，而门禁看不见。
- 决策层：
  - 契约校验遇到 `None`/`10**400` 会抛异常，而不是返回 blocker（`field_contracts.py:868, 889, 2098`）。
  - 每代重复深拷贝和哈希，约 160 ms。
  - 归档裁剪可能删掉仍被引用的帧（`decision_frame_archive.py:420-424`）。
  - bool/int 混淆（`decision.py:1479` 等）。
  - readiness 分层与注释相反。
  - 两套新鲜度阈值不一致。
- LLM：Responses API 解析不区分条目类型、不检查 status；控制器一次不可信的重新观察就判定终止；计划把 approving/executing 当作"未知目录"；比较运算对 bool、字符串、列表过于宽松。

**环境脚本**
- `start-live-profile.ps1:46` 的 ForbiddenRoot 检查因 PowerShell 解析方式永不触发（仍有 ExpectedUserName 兜底，✔）。
- `-WhatIf` 拦不住关闭游戏。
- PowerShell 与 Python 之间编码不一致，中文路径被替换成乱码。
- `restore-named-test-clone.ps1` 在 PS 5.1 上必然失败（依赖三参数的 `File.Move`）。
- 测试根与实时根的隔离只做字符串比较，8.3 短名可以绕过。

---

## 7. 测试与验证体系
1. **约 1.6 万行 C# 没有任何被执行的测试**，包括 ActionExecutor、ProbeWriter、ProbeStateReader 和全部读取器。`tools/*Harness` 只覆盖哈希、告警、字段契约和执行声明（约 2,500 行）。
2. **读取器的"验证"只是源码字符串匹配，外加对 Python 端的手写夹具测试**（例如 `scripts/verify_functional_building_flooded.py:84-95`）。所以 `occupied_cells` 恒为 null、可达性测的是拆除、Wire 不连通这类问题都不可能被发现，却仍产出了"seal"报告。
3. **测试与缺陷同错**：`enabledForDlc`（S2）；planned-owner 的测试把失败环节 mock 掉了（M1）。
4. **测试污染工作区**：
   - `scripts/test_controller_loop.py:252` 在 `dir=ROOT` 建临时目录；
   - 轮转监视器的测试在 `reports/l19/l20/flow/` 遗留了 111 个目录，ACL 只允许 SYSTEM/Administrators，当前用户打不开；
   - `test_rotation_observability.py` 在证据目录里建临时目录。
5. `verify_*.py` 会无条件覆盖共享报告（例如 `reports/functional-building-flooded-verification.json`）。
6. 10k 回放门限不稳，原因是夹具中所有快照的 ts 相同，而新鲜度按墙钟判断。在 cProfile 下只有 7,474/10,000 条被评估。

## 8. 工程与流程
- 本地工作区没有纳入版本控制（VPS 上有 git），大量"before-image / diff / hash"流程都靠手工维护。
- 三个环境管理脚本共 2.76 万行，而职责只是"备份 4 个配置、装 4 个文件、改 2 个配置、恢复存档"，大约 2–3 千行就够。复杂度让正式流程走不完，操作者转而使用临时脚本。
- `reports/` 有 3.6 GB、约 7.8 千个文件；过程日志文档多于设计文档。

## 9. 值得保留的设计
- 至多一次执行：派发前持久化执行声明，重启后遗留声明判为未知并锁存，不重放上一会话的 approved 命令，审批链"先原子回收再恢复"。
- Windows 字节锁经实测不会残留；仅创建发布加 inode 身份校验；journal 自哈希。
- 规范化哈希：非浮点值的键排序（UTF-8 字节序）、转义、代理对、NaN 拒绝与 C# 一致。
- 生存、告警、报表读取器的单位正确，排除了变压器，格子扫描有时间预算。
- 正式环境工具从不删除 ONI 用户数据，what-if 确实零写入，Stop 的时区问题已修复。

## 10. 建议路线（按优先级）
1. **用户数据**（第 2 节），需你确认后执行。之后测试会话一律禁用自动存档（`SaveGame.Instance.AutoSaveCycleInterval=0`），或把克隆放到独立目录；pre-boundary 必须在创建克隆之前拍摄；建立跨运行不变的"黄金基线"。
2. **小改动、高收益的修复**（每项多为几行到几十行）：
   - L1：帧摘要移除易变字段，或把 pending 绑定改为 (session, snapshot_id, state_hash, policy_hash)。
   - L2：两处 `evaluate_strategy` 使用同一个 goal。
   - L3：只在队列非空时打开文件；共享冲突做有界重试；零字节的打开失败不计为粘性故障。
   - L4：每轮加异常边界，失败时先发布失信 health 再退出；`run-bridge.ps1` 加 `exit $LASTEXITCODE`。
   - L5：`done/` 接受非变更命令的 `verified`。
   - P0-2：锁存按 lineage 作用域，并由 C# 在 `BeginSession` 时加载持久锁存。
   - L8、L9：`snapshot_seq` 不连续时重启事件周期；统一仲裁输入。
   - L11：前置条件要求 `requiredTech` 全部完成，或把后置条件改为"目标在队列中"。
   - G1：按路径顺序双向调用 `AddConnection`。
   - M2：统一运算符，条件上限按目录推导。
   - S2：改为修改当前 DLC 对应的 `enabledForDlc`，并修正对应验证脚本。
   - M6：Key 与 base_url 只取同一来源，强制 https。
3. **M1 最小闭环**（见 1.4）：新增候选生成器；模型只返回 candidate_id；异步调用加总截止时间；单叶子；走现有审批链；确认结果回写提示。
4. **止损**：冻结新增遥测；把 13 个读取器合并为一次遍历、稀疏、按实例索引的块，过滤 POI 并降频；精力转向收支速率和多世界覆盖。
5. **测试**：参照 `CommandContractsHarness`，用游戏 DLL 为 ActionExecutor 和 ProbeWriter 建可执行的测试工具；停用源码字符串式的验证脚本；测试不得写入工作区。
6. **流程**：本地 `git init`，取代手工哈希流程；精简环境脚本；把临时脚本移出工作区。

## 附：复现脚本位置（系统临时目录，可直接复跑）
- 决策层：`%TEMP%\oni_review\t1_cert_parity.py`、`t2_pending_blocks_suffocation.py`、`t7_gap_poison.py` 等
- Bridge：`%TEMP%\oni-review\exp_frames.py`、`exp_goal.py`、`exp_dir_swap.py`、`exp_evidence_scan.py` 等
- 托管层：`%TEMP%\oni_review_buz8o1az\gate_tests.py`、`approval_race.py`、`scan_race.py`、`share_test.py`
