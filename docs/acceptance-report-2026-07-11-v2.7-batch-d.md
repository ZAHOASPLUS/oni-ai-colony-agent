# V2.7 批次 D 验收报告

日期：2026-07-11

结论：`create_colony_plan`、`advance_colony_plan`、`pause_colony_plan`、`resume_colony_plan`、`abort_colony_plan` 和 `recover_colony_plan` 已通过状态机、原子 revision、故障注入和当前 ONI 会话控制面实测。六个动作均可从 `planned` 改为 `verified`，且没有进入游戏执行器白名单。

## 本轮已做

1. 新增 `oni.colony-plan.v1` 持久状态机，固定状态为 `draft -> active <-> paused -> completed|aborted|failed`。
2. 每次变更写出不可变 revision、前序哈希、原子 `current.json`、`active.json` 和根摘要；损坏尾 revision 会归档而不是静默覆盖。
3. 计划定义最多 32 步，只接受高级动作目录 A 至 C 的 game leaf，验证唯一 ID、DAG 依赖、cycle window、参数、前置条件和 expected effects。
4. `advance` 每次最多创建一个 `pending_human_approval` 命令；命令绑定 session/save/world/snapshot/state hash，并固定 `requires_approval=true`。
5. `pause` 不改写既有 pending/approved；`abort` 只隔离未批准 pending，发现 approved 时拒绝终止；`recover` 只恢复控制面，不创建、批准或补偿游戏命令。
6. done/failed/rejected/quarantine 与最新可信快照不唯一时进入 paused；只有 verified done 且 after_state 满足 expected effects 才推进检查点。

## 离线故障注入

| 用例 | 结果 |
| --- | --- |
| 两步生命周期 | create、advance、pause、resume、recover、verified checkpoint、下一 leaf、abort 全通过 |
| 表外 action | `set_paused` 作为 D leaf 被拒绝 |
| approved 终止 | abort 被拒绝，approved 文件和 open binding 保持不变 |
| current 损坏 | 从不可变 revision 1 恢复并写出新 revision |
| revision 尾损坏 | 损坏文件移入 `corrupt/`，从最后有效链恢复 |
| stale resume | 保持 paused，记录 `resume_revalidation_failed` |
| failed leaf | 计划和步骤均进入 failed |

`scripts/verify_colony_plan.py` 四组测试全部通过，且没有遗留 `.tmp` 文件。`verify_advanced_actions.py` 还证明六个 Bridge 控制动作与 `ALLOWED_ACTIONS` 完全不相交。

## 当前会话实测

| 项目 | 值 |
| --- | --- |
| 会话 | `session-20260711-052929-eaf80c15` |
| 存档哈希 | `2471f42f8a3693215c9d3eab760c71b117a4ec4f1603f41445390fd7a9bfc65f` |
| 计划 | `plan-20260711-055131-0322d34c` |
| revision | `1 -> 6` |
| 候选命令 | `cmd-plan-22633422a874c7f36a3a` |
| 最终状态 | plan `aborted`，command `quarantine` |
| 游戏执行 | `false` |

实测在游戏暂停、Bridge 健康、动作策略关闭的条件下依次执行六个计划控制 action。候选命令始终带 `requires_approval=true`，从未进入 approved/done/failed，也没有产生游戏 ack；abort 后仅进入 quarantine。

## 下轮要做

1. 安装并重启最终 Probe `0.6.1`，验证候选扫描增量预算消除周期性主线程尖峰。
2. 在同一最终构建证据集中重跑 A、B、C 行为或汇总其当前构建兼容证据，并完成 10 分钟性能门槛。
3. 运行完整 Python、C#、Bridge、LLM、`game_verify` 正例和已知失败矩阵。
4. 无保存退出 ONI、停止 Bridge、复核所有存档哈希、策略关闭和无残留进程，再发布 V2.7 总验收报告。
