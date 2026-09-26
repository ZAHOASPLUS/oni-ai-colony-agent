# A09A7 colony-plan custody verifier

本次只新增 `scripts/verify_colony_plan_custody_integration.py`，不修改生产模块或旧 verifier。脚本是四 case、离线、source-freeze 后运行的证据工具：

1. `valid_plan_lifecycle_baseline` 使用旧 `verify_colony_plan` 的 `state`、`write_live_state`、`priority_definition` 构造真实受信 fixture，并实际走 `create_colony_plan -> advance_colony_plan -> abort_colony_plan`。它检查 draft/active/aborted、唯一 approval-gated pending、以及精确 id/source/reason 的唯一 quarantine。
2. `lifecycle_lock_excludes_advance` 只创建 draft；父进程先取得真实 `command_custody.lifecycle_custody`，再以 multiprocessing `spawn` 子进程调用真实 `advance_colony_plan`。Pipe 加 ready/pre-call 握手，父锁内以不超过 50ms 的间隔观察 0.8 秒，记录各 queue stage 的字节数、SHA256、inode/dev、mtime，以及 plan revision/pointer/hash；每个实际样本都计入 changed/error 计数，瞬态变化也失败。释放后要求子进程正常返回 active 和唯一 pending。
3. `lifecycle_lock_excludes_abort` 真实 create/advance 后使用同样边界调用真实 abort；锁内 pending 与 plan 不得变化，释放后要求正常 aborted 和唯一 quarantine。子进程在 pre-call 后才进入 API，不构造等待父锁的自锁死结。
4. `stale_quarantine_payload_rejected` 无子进程。先在本 case Temp 内的独立 positive-control 子 root 用同构合法 priority B 和原 `write_json_atomic`，以新鲜 B 作为 expected 调真实 `_quarantine_pending_command`，要求成功且只有一个 quarantine；正对照失败则不执行主断言。随后保存主 root pending A 的深拷贝，再用原 writer 替换为同 id、合法 schema/status/source、合法 0--5 priority 的 B。真实 `_quarantine_pending_command(..., A, plan_id)` 必须抛 `ColonyPlanError` 或 `CommandCustodyError`；B 的完整字节、hash、identity 留在 pending，quarantine 不得拥有该 id，plan 不得变成 aborted。

脚本为每个 case 使用独立 `TemporaryDirectory`；ExitStack 在临时目录删除前注册并运行自有 child shutdown，join 有界，无法确认清理时不会伪报 `residual=false`。report 目录在任何 fixture 前以 UTC+pid+随机后缀分配，`report.json` 使用 `x` 模式；报告只含 compact hash/count/diff，不含 queue 文件正文。source preflight 会核对实际模块路径必须落在当前 workspace；source before/after hash 覆盖 verifier、colony plan、command custody、queue、action outcomes、legacy、policy、decision、frame 及其 fixture 依赖。路径不匹配或 hash 采集失败时四个同名 case 全部 blocked，不创建 fixture 或 child；baseline 失败时 case 2--4 同样 blocked。任一 setup 异常、cleanup、source drift、非零/终止/残留 child、缺 case 或零 assertion 都使 overall exit 1。

当前阶段只允许内存 compile 和静态检查，未做功能运行验证、未进行 live admission，也未访问真实 queue、存档、Mod、游戏、Steam 或网络。下一步由 root 在 source-freeze 后先运行本 verifier，取得旧源的失败基线，再另派生产修改。

保留两个未解设计边界：core transition 的 `expected_source` 将 canonical JSON 与原始 bytes 比较，而旧 `cp.write_json_atomic` 使用 pretty JSON，直接把 abort 改为该 API 可能出现兼容性失败；advance 的二次 context 变更目前 `unlink`，安全隔离后保留 id 会影响重试。本 verifier 不解决这两点，也不增加第五个 scope。
