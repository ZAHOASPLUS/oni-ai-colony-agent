# M1 第一轮实盘测试清单（给 ChatGPT 客户端）

日期：2026-10-27 Asia/Tokyo。适用分支 `review-fixes-20260926`，代码不早于 `ef1f0dd`。

本清单把 B7（暂停节奏）、B15（电解器供给链）、B6（温度与加热器）和 B16（检查点）的实盘核对项合并成**一轮**：一次准备、一个控制器会话、一次停止、一次收尾。通用流程（模型配置、离线测试、冒烟测试）见 [test-handoff-chatgpt-20261027.md](test-handoff-chatgpt-20261027.md)；所有实盘规则以 `AGENTS.md` 为准。

## 0. 这一轮测什么、不测什么

- 一轮只做一次实盘尝试。尝试失败时不在本轮重试，把证据交回来，修正以后再开下一轮。
- **B16 跨两轮验证**：本轮停止时存出第 1 个检查点，这只能证明“存得下来”。“下一次从检查点启动、进展还在”放在下一轮的准备阶段核对（第 7 节）。
- 下面这些项依赖游戏里实际出现的情况，**没出现就记“未触发”，不算失败**：
  - Terra 开局有藻类，模型多半走扩散器路线，可能根本不建电解器，B15 那几项就不会触发；
  - 不一定会出现失温，B6 的告警、缓解和加热器那几项就不会触发。
- 控制器调用的模型：Cockpit 的 6.1 terra 或 6.1 sol（第 2 步配置）。最终验证用的 DeepSeek 不在本轮。

## 1. 前置条件（任何一条不满足就不启动）

| # | 条件 | 怎么确认 |
|---|---|---|
| P1 | 用户已建好 Terra 源殖民地 `m1-terra-src`，并按命名克隆流程做出克隆；克隆的 `.sav` 和预览 `.png` 都已验证，克隆清单存在 | 克隆清单文件和两个哈希 |
| P2 | 用户原存档和正常 Mod 配置的哈希已记录（不可变输入） | `py -3 -B scripts/save_tree_baseline.py capture reports/m1/runs/<run_id>/pre-baseline.json` |
| P3 | 游戏未运行，没有残留的控制器或 Bridge 进程 | 进程列表 |
| P4 | 离线测试：83 个模块中只有已知的 5 个不是 OK（见交接文档第 2 节） | 逐模块运行的输出 |
| P5 | 模型冒烟测试通过，`config.provider` 为 `cockpit`，`config.config_error` 为空 | `py -3 -B scripts/verify_oni_model_integration.py` |
| P6 | 用户不在操作电脑（运行期间不能有 GUI 输入，也不能改变游戏窗口大小） | 和用户确认 |
| P7 | `scripts/test-codex-transport-stability.ps1` 显示最近没有网络断开 | 脚本输出 |

## 2. 准备

1. **部署本轮的 Mod 构建**。B15、B6 和加热器都需要新的 Probe：
   - 记下当前已部署的 DLL 哈希，以备收尾时还原。2026-10-27 看到的是 `452916a5…`。路径：`%USERPROFILE%\Documents\Klei\OxygenNotIncluded\mods\Dev\oni-ai-colony-agent\OniAiProbeMod.dll`。把原文件复制到 `reports/m1/runs/<run_id>/mod-backup/`。
   - 把 `reports/m1/mod-builds/b6b-ef1f0dd/` 里的 `OniAiProbeMod.dll` 和 `.pdb` 复制到上面的目录。用 `SHA256SUMS` 核对：DLL 应为 `5fc69e60cb5b82c8…`。
   - 这份构建由 `0e61287` 的 Mod 源码编出，到 `ef1f0dd` 为止 `mod/` 没有改动。
2. **配置模型**：`powershell -File scripts/configure-oni-model.ps1 -Provider cockpit -Model <Cockpit 列出的模型 ID>`，再用 `-Show` 核对。
3. **绑定**（游戏关闭时）：
   ```powershell
   py -3 -B scripts/prepare_autonomous_run.py binding --clone-manifest <克隆清单> --clone-save <克隆 .sav> --checkpoints
   ```
   第一轮还没有检查点，绑定的就是克隆本身。
4. **设置加载指针**：`py -3 -B scripts/set_oni_load_save.py --latest-checkpoint <克隆清单> <克隆 .sav>`。
5. **发布启动检查点说明**（见第 3 节），然后启动游戏，载入绑定的存档。
6. **写配置**：`py -3 -B scripts/prepare_autonomous_run.py config --run-id <run_id>`。它会把 `checkpoints: true` 写进配置；出现 `error` 就停下，不要手改配置。按输出里的 `next` 写自主策略。
7. **启动控制器**，单独一条短命令：
   ```powershell
   powershell -File scripts/start-controller.ps1 -Config <配置文件> -LogDir reports/m1/runs/<run_id>/logs
   ```

## 3. 启动前的检查点说明（照这个格式发）

```
action_id: m1-r01-launch
前置条件（刚核对过）：P1–P7 全部满足；已部署 DLL = 5fc69e60…；绑定存档 = <克隆 .sav 哈希>
唯一动作：启动游戏并载入绑定存档，随后启动控制器
预期证据：Probe session_started；autonomous-controller.json 的 phase 进入运行；model-loop.json 开始更新
回滚：停止控制器 → WM_CLOSE 关闭游戏 → 还原 DLL 和加载指针 → 对比存档基线
```

不确定某个动作有没有发出去时，不要重发。先读进程、队列、日志游标和结果状态。

## 4. 运行中观察（B7）

只用 `py -3 -B scripts/watch_controller.py --seconds 25` 观察，每次最多 30 秒。观察命令要和启动命令分开。

**运行时长**：游戏时间推进约 2 个周期就停止（第 5 节）。如果墙钟超过 60 分钟、出现阻塞项或紧急告警持续不退，也停止。

| # | 核对项 | 在哪看 | 通过标准 |
|---|---|---|---|
| B7-1 | 运行窗口 | `model-loop.json` 的 `pause_cadence`，运行时为 `{"state": "running", "ran_cycles", "window_cycles": 0.25}` | `ran_cycles` 到 0.25 左右就暂停，不会长时间不停 |
| B7-2 | 暂停和恢复由规则发出 | `last_event.kind == "rule_choice"`，`rule` 为 `rule:pause-cadence.v1`；对应 done 命令证据里的 `model` 也是这个规则名 | 每次暂停、恢复都是这种形状，模型没有直接选 `set_paused` |
| B7-3 | 暂停时的改动数 | 暂停时 `pause_cadence` 为 `{"state": "paused", "actions", "max_actions": 3, "model_waited", "resume_offered"}` | 每次暂停在 `actions` 达到 3、模型选择 `wait` 或没有可改动的事情时恢复。**记录每次暂停的 `actions`**，用来算平均值 |
| B7-4 | 长时间停在暂停 | 同上 | 如果 `resume_offered` 为 false，就是有紧急告警拒绝恢复，属于预期行为，记下是哪个告警；如果为 true 但一直不恢复，就是问题 |
| B7-5 | 暂停命令的占比 | Bridge 目录 `commands/done/` 里的命令 | `set_paused` 不超过 done 命令的 25%（M1 的退出条件之一）。Phase 2 是 59% |
| B7-6 | 游戏时间推进速度 | `game_time_cycles` 随墙钟的变化 | 记录“每 10 分钟墙钟推进多少周期”，用来和 Phase 2 对比 |

## 5. 停止与存档（B16 第一半）

1. 发检查点说明：`action_id: m1-r01-stop`，唯一动作是 `py -3 -B scripts/oni_autonomous.py stop --config <配置文件>`。
2. 用 `watch_controller.py` 观察 `autonomous-controller.json`：`phase` 会依次经过排空、`checkpointing`，最后到 `stopped`。超时上限为 120 秒。

| # | 核对项 | 通过标准 |
|---|---|---|
| B16-1 | `checkpoint.phase` | 为 `verified`。如果为 `failed`，记下 `checkpoint.error`；停止仍然会完成，但不会留下检查点 |
| B16-2 | 存档之后 Bridge 还在发布可信快照 | 存档后 `latest_state.json` 的 `snapshot_seq` 仍在增长，`bridge.trust_ready` 为 true。这是 B16 设计里排第一的风险：如果 Bridge 不再发布，状态机会在 120 秒后记为超时 |
| B16-3 | 检查点文件 | 克隆文件夹里出现了 `<clone>-ckpt-001.sav` 和 `.manifest.json`，清单里的 `.sav` 哈希与文件一致，父哈希就是克隆的哈希 |
| B16-4 | 存档的副作用 | 拿收尾时的保护基线对比，列出克隆文件夹以外**所有**有变化的文件。重点看两项：`SaveColonySummaryData` 写了哪些殖民地摘要文件；`kplayerprefs.yaml` 是否因为 `updateSavePointer` 而改变 |

3. 控制器停止后，用 WM_CLOSE 关闭游戏，不要用鼠标或键盘操作游戏。

## 6. 事后核对（B6、加热器、B15）

候选集的备注（notes）只发给模型，不写进 `model-loop.json`。所以停止后用只读工具，在这次会话的录制上重新生成候选集：

```powershell
py -3 -B scripts/review_live_candidates.py <bridge 目录>/sessions/<session>/state.jsonl --policy <run 目录>/action-policy.json > reports/m1/runs/<run_id>/candidate-review.json
```

快照很多时可以加 `--stride 5`（只影响候选的抽样，耗时统计仍然覆盖所有快照）。Phase 2 的一份 157 MB 录制，用 `--stride 10` 大约 2 秒跑完。卡住蓝图的取消候选依赖录制里没有的放置时长，这一项可能和实盘不同，其他都和控制器运行的是同一套生成器。

### Probe 字段（新构建是否生效）

| # | 输出里的字段 | 通过标准 |
|---|---|---|
| F-1 | `probe_fields_present.duplicant_temperature_state` | 等于抽样数（例如 `40/40`）。为 `0/N` 说明部署的不是新构建，**后面各项都不用看了** |
| F-2 | `probe_fields_present.dig_temperature_c` | 有挖掘标记时大于 0 |
| F-3 | `probe_fields_present.space_heater_catalog_unlock`、`space_heater_count` | 等于抽样数 |
| F-4 | `probe_fields_present.supply_links` | 等于抽样数（没有可连的建筑时，这个字段也应该存在，只是内容为空） |
| F-5 | `read_metrics_ms_blueprints` | 记录中位数和最大值。旧版 Probe 在 Phase 2 是 3.86 ms 和 13.6 ms。如果明显变慢，说明连线搜索或 `GetTechFromItemID` 的开销过大（B15 风险 6、B6 风险 10） |

### 温度（B6），只在出现 `hypothermia` 或 `hyperthermia` 时核对

| # | 核对项 | 通过标准 |
|---|---|---|
| T-1 | `notes` 里有 `temperature_alert:<告警>:exposed=<n>` | 告警开着的快照都有。如果是 `exposed_unobserved`，说明复制人的温度字段缺失 |
| T-2 | `candidate_families.priority_dig` 和第一次出现的时间 | 暂停时出现。注意：这个计数也包括一般的 Dig 优先级候选，要对照 `model-loop.json` 里候选的 summary 区分 |
| T-3 | Dig 优先级的下限 | 从命令证据里看：同一个复制人的 Dig 不会被降到 2 以下 |
| T-4 | 挖掘引导 | 有告警时，模型选中的 `dig_*` 候选摘要里带温度范围，并且没有选过冷（低于 −20 °C）的段 |
| T-5 | 降低 Dig 后失温是否消失 | 对照告警消失的时间和降 Dig 的时间。**只记录，不判定成败**，这是要实盘回答的问题（B6 风险 3） |

### 加热器，只在出现 `hypothermia` 时核对

| # | 核对项 | 通过标准 |
|---|---|---|
| H-1 | 目录项 | 最后一个快照的 `blueprints.catalog.SpaceHeater` 给出 `unlocked`、`research_tech_id` 和 `research_prerequisites_complete`。**记下 `research_tech_id` 的实际值**（B6 风险 6） |
| H-2 | 解锁研究 | 未解锁且前置已完成时，有 `research_<tech>` 候选；前置未完成时有 `unlock_research_blocked:*` 备注；出现 `unlock_research_complete_still_locked:*` 说明这个世界不提供加热器，记下来 |
| H-3 | 放置 | 已解锁、并且失温时，出现 `space_heater` 候选。如果被选中，执行器接受，并且最后建成了（`final_buildings.SpaceHeater.built` ≥ 1） |
| H-4 | 加热器的供电 | 加热器建成后出现第二台人力发电机的候选，以及一条通往加热器的 `power_link_*` |
| H-5 | 位置和效果 | 记录加热器离复制人活动区有多远，以及建成后失温多久消失（B6 风险 7、9），只记录 |

### 电解器供给链（B15），只在建了电解器时核对

| # | 核对项 | 通过标准 |
|---|---|---|
| S-1 | 发电机和泵 | 电解器建成后，出现 `manual_generator`、`liquid_pump` 候选 |
| S-2 | 连线的顺序 | 泵建成之前没有 `power_link`，并且有 `power_link_waits_for_liquid_pump` 备注；泵建成之后，有一条同时连到电解器和泵的 `power_link` |
| S-3 | 执行器是否接受 | `power_link_*`、`liquid_link_*` 被选中后，命令进入 `done`，而不是被拒绝。**被拒绝时记下拒绝原因**：这可以直接回答 B15 风险 1，即电线或管道蓝图能不能放在已建成建筑的端口格上 |
| S-4 | 运转 | `electrolyzer_first_operational` 不为 null，也就是 `life_support_buildings` 里电解器的实例为 `operational: true` |
| S-5 | 卡住的连线 | 如果出现 `cancel_blueprint_path_*`，记下是哪条路径、为什么没人开工 |

## 7. 下一轮开头（B16 第二半）

下一轮准备时：

1. 绑定时照样加 `--checkpoints`，它应该绑定 `ckpt-001`，而不是克隆本身。
2. `set_oni_load_save.py --latest-checkpoint` 把加载指针指向 `ckpt-001`。
3. 游戏载入后，Probe `session_started` 里的游戏周期**不早于**本轮存档时的周期（本轮 `checkpoint` 记录和检查点清单里都有）。这一条满足，B16 才算通过。
4. 如果故意改成加载克隆或较旧的检查点，`load_config` 会拒绝。这一项离线已经测过，实盘不必专门测。

## 8. 收尾（按 AGENTS.md）

1. 确认游戏和控制器都正常退出，没有残留的 helper 进程；命令队列中没有非终态的命令。
2. 用 `mod-backup/` 里的文件还原已部署的 DLL，并核对哈希回到部署前的值。是否让新构建常驻，等用户决定。
3. 存档基线：`py -3 -B scripts/save_tree_baseline.py capture <post>.json`，然后 `compare <pre>.json <post>.json`。预期只有克隆文件夹里新增的 `-ckpt-001.sav` 和清单。其他变化全部列出，B16-4 也要用到。用户原存档的哈希必须不变。
4. 加载指针：本轮和下一轮之间保持指向 `ckpt-001`。只有在用户要求恢复到用户自己的存档时，才改回原值。

## 9. 交回的结果

把下面内容写进 `reports/m1/runs/<run_id>/round-report.md`，并在对话里只贴这一份。大日志放在证据文件里，回报中只给路径、哈希和计数。

```
run_id / 模型 ID / 部署的 DLL 哈希 / 起止游戏周期 / 墙钟时长
前置条件 P1–P7：逐条 ok 或说明
B7-1..6：逐条 通过 / 不通过 / 未触发，附数值（平均每次暂停的改动数、set_paused 占比、每 10 分钟推进的周期）
B16-1..4：同上，附 checkpoint 记录和副作用文件清单
F-1..5、T-1..5、H-1..5、S-1..5：同上；candidate-review.json 的路径和哈希
执行器拒绝过的命令：candidate_id、动作、拒绝原因（逐条）
收尾：进程、队列、DLL 还原、存档基线对比结果
异常和疑点：任何与本清单预期不符的现象
```
