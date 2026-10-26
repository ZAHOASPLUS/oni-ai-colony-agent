# B2 排查：Phase 2 电解器蓝图的生命周期

日期：2026-10-27 Asia/Tokyo。离线排查，没有启动游戏。路线图条目：[roadmap-20261027.md](roadmap-20261027.md) 的 B1、B2。

## 结论

1. **电解器蓝图“消失”是因为施工完成，不是因为被取消。**最后一个会话里放置了 7 个电解器蓝图：1 个在 0% 时被取消，另外 6 个都有复制人施工，进度最高到 92–93%，随后在游戏运行中消失。最终状态里正好有 6 座建成的电解器。
2. **“2×2/2×3 多列建筑从不开工”这条已知限制不成立。**2×2 的电解器能正常建成；2×3 的微生物榨汁机在整个 Phase 2 里**一次都没有被放置过**，所以对它没有任何结论。
3. **真正的问题是电解器建成后不可能运转。**电解器需要电力，而且只能从液体管道取水；动作目录里没有管道或泵，Phase 2 也从没铺过电线。6 座电解器全部 `operational: false`。
4. **模型看不到自己已经建了电解器。**`buildings` 完工计数只统计扩散器、厕所和人力发电机，于是同一条产氧路线被重复建了 6 次。
5. **每次重启游戏都会从克隆存档重新加载。**各会话开始时的游戏时间依次是 15.8、11.7、10.0、6.4 周期，Phase 2 的进展没有跨进程累积。

## 证据

### 数据源

| 数据 | 路径 | 说明 |
|---|---|---|
| 最后一个会话的完整 Probe 日志 | `reports/phase2/runs/dsh-20261025-r01/logs/session-20261026-113017-state.jsonl.gz`（原文件 SHA-256 `54e5c99b…a713a9`） | 2026-10-26 11:30–12:19，共 586 个快照。原文件在 Klei bridge 的 `sessions/session-20261026-113017-22be51f4/`，下次运行会被覆盖，所以已经压缩归档 |
| 已完成命令 | `reports/phase2/runs/dsh-20261025-r01/final-evidence-20261026-122529/commands/done/` | 7 次电解器放置，2 次取消（只有 1 次针对电解器） |
| 最终状态 | 同目录 `latest_state.json` | `life_support_buildings`、`buildings`、蓝图目录 |
| 重建结果 | `reports/phase2/b2-blueprint-lifecycle-20261027.json` | 由 `scripts/inspect_blueprint_lifecycle.py` 生成 |

复现命令：

```bash
py -3 scripts/inspect_blueprint_lifecycle.py reports/phase2/runs/dsh-20261025-r01/logs/session-20261026-113017-state.jsonl.gz
```

### 蓝图时间线（最后一个会话，游戏时间从 6.41 周期开始）

| 蓝图 | 建筑 | 格 | 出现 → 消失 | 最高进度 | worker | 消失时 | 对应的取消命令 |
|---|---|---|---|---|---|---|---|
| `id_14588` | MineralDeoxidizer | 139386 | 11:31:01 → 11:39:16 | 0 | — | 暂停中 | 有（11:39:11） |
| `id_15607` | MineralDeoxidizer | 147025 | 11:36:51 → 11:43:26 | 80 | 80 | 运行中 | 无 → 建成 |
| `id_16394` | Electrolyzer | 147028 | 11:44:32 → 11:45:42 | 0 | — | 暂停中 | 有（11:45:34，放置后约 66 s） |
| `id_16395` | Electrolyzer | 147028 | 11:49:37 → 11:51:54 | 92 | 80 | 运行中 | 无 → 建成 |
| `id_16708` | Electrolyzer | 147016 | 11:56:10 → 11:59:25 | 40 | 82 | 运行中 | 无 → 建成 |
| `id_16709` | Electrolyzer | 147018 | 11:57:25 → 11:59:05 | 71 | 82 | 运行中 | 无 → 建成 |
| `id_16885` | Electrolyzer | 147020 | 12:04:57 → 12:06:43 | 93 | 84 | 运行中 | 无 → 建成 |
| `id_17004` | Electrolyzer | 147023 | 12:11:19 → 12:15:59 | 78 | 82 | 运行中 | 无 → 建成 |
| `id_17005` | Electrolyzer | 156553 | 12:14:29 → 12:16:04 | 46 | 80 | 运行中 | 无 → 建成 |

快照间隔约 5 s，所以记录到的最高进度低于 100%。施工完成后，蓝图在下一个快照里就不在了。

### 建成的建筑（最终状态，`life_support_buildings`）

| 建筑 | 实例 | functional | operational | 说明 |
|---|---|---|---|---|
| MineralDeoxidizer | 16111、16338 | true | false | 请求藻类，世界里藻类为 0 |
| Electrolyzer | 16565、16724、16753、16893、17025、17031 | true | false | 6 座，与 6 个完成的蓝图一一对应；没有物品请求（水只能走管道） |

同一份状态里，`buildings` 只有 `oxygen_diffuser: 2`、`outhouse: 7`、`manual_generator: 1`。`functional_buildings.prefab_ids` 也不包含 Electrolyzer。来源是 `mod/Source/Probe/ProbeStateReader.cs` 的完工计数，只匹配 OxygenDiffuser/MineralDeoxidizer、Outhouse、ManualGenerator。

### 电解器为什么不运转

用 `tools/OniIlDump` 读取当前安装的 `Assembly-CSharp.dll`，`ElectrolyzerConfig` 中有以下设置：

- `BuildingDef.RequiresPowerInput` 和 `PowerInputOffset`：需要接电。
- `BuildingDef.InputConduitType`、`UtilityInputOffset`，以及 `ConduitConsumer`（`capacityTag` 为 Water）：水只能通过液体管道输入，不能靠复制人搬运。

当前动作目录的公用设施白名单只有 `Wire`（`agent/advanced_actions.py` 的 `UTILITY_BLUEPRINT_ALLOWLIST`），没有液体管道和泵。Phase 2 的 done 命令里也没有任何电线放置。所以在现有动作目录下，电解器无论建多少座都不可能产氧。

### 每个会话都从存档重新开始

| 会话开始（2026-10-26） | 游戏时间（周期） |
|---|---|
| 10:32 | 15.78 |
| 11:10 | 11.66 |
| 11:28 | 9.96 |
| 11:30 | 6.41 |

周期在倒退，而且 `id_14588` 这个旧蓝图在每个会话开头都在。这说明每次启动游戏都从同一份克隆存档加载；AutosaveGuard 为了保护克隆而阻止保存，所以进程之间不保存进展。active-goal 也把“保存/重新绑定检查点”列为未完成。

## 为什么之前会误判

- 第 49–51 轮的观察脚本大约每 55 s 采样一次。电解器从放置到建成只要约 1.5–4.5 分钟，采样往往只捕捉到“刚放置、进度 0”，下一次采样时蓝图就已经不在了，于是被记成“放置 → 取消，progress 始终 0”。
- 判断“有没有建成”用的是 `buildings` 计数，而这个计数不包含电解器。
- 报告把“消失”直接写成了“被取消”，没有和 done 目录里的取消命令核对。

## 对路线图的影响

- B1 改为“已澄清”：多列建筑能够建成；微生物榨汁机没有测过。
- B2 关闭：消失 = 施工完成（6 次），或取消命令（1 次）。
- 新增 B14（完工计数漏掉了电解器等建筑）、B15（电解器缺少管道供水和供电，动作目录没有管道/泵）、B16（进展不跨进程保存）。
- B3（取消没有年龄门槛）仍然成立：被取消的那个电解器只存在了约 66 s，而其他电解器从放置到建成要 1.5–4.5 分钟。
