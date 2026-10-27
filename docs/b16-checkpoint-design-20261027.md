# B16 设计：让克隆进展跨进程延续

日期：2026-10-27 Asia/Tokyo。状态：**设计已定（D4 选 b）；清单链、选择逻辑、停止时存档和从最新检查点加载均已离线实现并自测（2026-10-27）；实盘验证未做**。路线图条目：[roadmap-20261027.md](roadmap-20261027.md) 的 B16。

## 问题

Phase 2 每次启动游戏都从同一份克隆存档加载，会话起始周期依次是 15.8 → 11.7 → 10.0 → 6.4（[b2-blueprint-lifecycle-20261027.md](b2-blueprint-lifecycle-20261027.md)）。AutosaveGuard 为了保护用户存档而关闭了自动存档，控制器也从不主动保存，所以一个进程里的进展在进程结束时全部丢失。M1 的退出条件要求连续 2 个周期满足生存指标，M2 要求至少 10 个周期，进展不能跨进程保存的话，这些都无从谈起。

## 已有的能力

| 组件 | 现状 | 位置 |
|---|---|---|
| Mod 侧 `save_checkpoint` 控制 | 已实现：读取受策略约束的控制文件，调用 `SaveLoader.Save(target, false, true)`，校验写入代数、结果路径、活动路径和存档哈希，结果写入 ack | `mod/Source/Probe/ValidationCheckpointController.cs` |
| Mod 侧 `reload_checkpoint` 控制 | 已实现，带 reload-intent 恢复 | 同上 |
| 动作队列静止检查 | 已实现：有未完成的命令时拒绝存档 | 同上 |
| 控制策略 | `oni.validation-control-policy.v1`：`allowed_operations`、`allowed_save_hashes`、`allowed_save_path_ids`、`allowed_target_save_path_ids`、`expires_at` | `config/validation-control-policy.example.json` |
| 发起方 | `scripts/manage_validation_checkpoint.py`（save/reload，写策略、写控制、等结果） | — |
| 验证器 | `scripts/verify_validation_checkpoint.py` | — |
| 保存路径转换授权 | 目标路径与当前存档不同时，`StartupBinding.AuthorizeSavePathTransition` 临时放行 | `StartupBinding.cs` |
| 自动存档保护 | AutosaveGuard 在每次保存前重新写入 0，所以检查点里存的也是 0 | `AutosaveGuard.cs` |

**结论：存档能力已经有了，缺的是把它接进自主运行流程，以及检查点的身份管理。**

## 已核实的约束（2026-10-27 读取 `Assembly-CSharp.dll` 的 IL）

1. **`SaveLoader.Save(filename, isAutoSave, updateSavePointer)` 只写 `.sav`，不生成 `.png` 预览。**这个方法里出现的 `.png` 只用于自动存档轮换时删除旧截图。而 AGENTS.md 规定只能加载 `.sav` 和 `.png` 都已验证的命名克隆，所以检查点本身不满足这条规则。
2. **`SaveLoader.Save` 会调用 `RetireColonyUtility.SaveColonySummaryData`。**它可能在存档文件夹之外写殖民地摘要数据。克隆与用户原存档共用同一个殖民地身份，所以这个调用有可能改动属于用户原殖民地的数据。**实现前必须用一次受控的存档核实它写到哪里**；如果它会碰保护范围内的文件，就要把这些文件纳入保护基线的哈希和还原。

## 方案

### 检查点身份

- 检查点只写在克隆自己的存档文件夹里，文件名带编号：`<clone>-ckpt-<NNN>.sav`。绝不覆盖原始克隆 `.sav`，也绝不写到用户原存档所在的文件夹。
- 每个检查点配一份清单 `<clone>-ckpt-<NNN>.manifest.json`，内容包括：`.sav` 的 SHA-256、父检查点（或原始克隆）的哈希、`run_id`、写入时的会话 ID 和游戏周期、写入时的队列快照，以及“预览”字段（见下）。
- 预览：检查点没有游戏生成的 `.png`。**用户已于 2026-10-27 选择 (b)**，下面两个选项保留作记录：
  - (a) 复制父克隆的 `.png` 作为占位，并在清单里写明“这是继承的预览，不是这个存档的截图”；
  - (b) 修订规则，让检查点以“`.sav` 哈希 + 清单链”代替 `.png` 作为身份证明。

### 什么时候存档

- 只在安全点存档：所有命令队列都处于非终态为空的状态（Mod 已经会检查），并且没有未解决的未知结果。
- 时机有三种：
  - `oni_autonomous.py stop` 排空命令后、关闭游戏前；
  - 每过 N 个游戏周期（草案 N = 1）；
  - 一个生命维持建筑首次运转后（里程碑式进展）。
- 存档失败只记录，不重试同一个控制；下一个安全点再试。

### 下一次启动

- `prepare_dsh_autonomous_run.py` 这一类准备脚本改为：选择最新的、清单链完整且哈希核对通过的检查点作为加载目标（`set_oni_load_save.py`），而不是原始克隆。
- 启动锚定（startup expectations）里的存档身份改为该检查点的哈希。

### 收尾与还原

- 保护基线里，克隆文件夹中新增的 `*-ckpt-*.sav` 和清单属于“已批准的新增”，要和 Phase 2 对克隆三件套的处理方式一致：列出、记哈希、不算对原存档的改动。
- 原始克隆 `.sav`/`.png`/清单和用户原存档的哈希在收尾时必须不变。
- 如果核实发现 `SaveColonySummaryData` 写到了保护范围内，就必须在收尾时把那些文件还原，并在基线里提前记下它们的哈希。

## 实现顺序（M0 之后、M1 之内）

1. 一次受控实盘核实：在测试克隆上执行一次 `save_checkpoint`，记录 `SaveLoader.Save` 前后克隆文件夹、用户存档文件夹和殖民地摘要目录的完整文件清单与哈希差异。**这一步是实盘操作，要等用户在场并完成 D1。**
2. 检查点清单格式和“选择最新检查点”的逻辑（离线，可测试）。
3. 把存档接入 `oni_autonomous.py stop` 和周期性安全点。
4. 准备脚本改为从检查点加载；收尾脚本识别检查点新增。
5. 一次实盘测试：会话 A 推进并存档，会话 B 从检查点启动，起始游戏时间不早于会话 A 的存档时间。

## 需要用户决定

| ID | 决策 | 默认建议 |
|---|---|---|
| D4 | 检查点没有游戏生成的 `.png`：复制父克隆的 `.png` 作为占位（a），还是修订规则，以清单链作为身份证明（b） | (b)：占位图会让人误以为它是这个存档的截图，清单链能证明的东西更多 **→ 用户选择 (b)** |

## 实现记录（2026-10-27，离线）

| 部分 | 位置 | 说明 |
|---|---|---|
| 清单链与最新检查点选择 | `agent/checkpoint_chain.py` | `select_latest_checkpoint`、`build_checkpoint_manifest`、`publish_checkpoint_manifest`；新增 `loader_target`、`match_loader_anchors`、`loader_path_id_upper` |
| 停止时存档 | `scripts/stop_checkpoint.py`，由 `scripts/oni_autonomous.py` 的 `stop_requested` 驱动 | 已停止、队列静止、策略封存之后，发出 `save_checkpoint` 控制，目标是 `<stem>-ckpt-<n+1>.sav`；每轮 Bridge 迭代推进一步，不阻塞；结果确认、再看到一份更晚的可信快照之后，才发布清单并结束停止；被拒、失败或超时（120 s）都只记录，不重试，停止照常完成 |
| 只接受最新一环 | `oni_autonomous.load_config` 的可选字段 `checkpoints: true` | 加载锚点必须是链上最新的一环（没有检查点时就是克隆本身）；加载克隆或较旧的检查点都会被拒绝；不带该字段时行为不变 |
| 准备与加载指针 | `prepare_dsh_autonomous_run.py --checkpoints`、`set_oni_load_save.py --latest-checkpoint MANIFEST SAVE` | 启动绑定和游戏的 `SaveFilenameKey/` 都指向最新一环 |
| 测试 | `scripts/test_checkpoint_chain.py`（16）、`scripts/test_stop_checkpoint.py`（12） | 用假 Mod 跑完整流程；检查点 1 之后再存检查点 2；被拒、超时、策略未封存、存档 dirty、目标已存在；加载锚点 |

尚待实盘确认的风险：

- **Bridge 在存档后能否继续发布可信快照**：同一会话里存档哈希变成新检查点后，启动绑定严格模式下的 Bridge 是否仍把快照判为可信。确认存档用的正是这份快照；如果 Bridge 不再发布，状态机会在 120 秒后记为超时失败，停止仍会完成，但不会留下检查点。
- `SaveColonySummaryData` 写到哪里（见上文“已核实的约束”第 2 条）。
- `updateSavePointer=true` 会让游戏把“上次存档”指向新检查点，`kplayerprefs.yaml` 因此会变化；收尾还原时要把它考虑在内。
- ~~`prepare_dsh_autonomous_run.py` 仍按 Phase 2 的目录结构定位克隆~~ → 已由 `scripts/prepare_autonomous_run.py` 解决（2026-10-27）：克隆清单和 `.sav` 作为参数，`binding --checkpoints` 绑定最新一环；`config` 阶段若发现绑定之后又出现了更新的检查点，会拒绝写出能加载的配置。
