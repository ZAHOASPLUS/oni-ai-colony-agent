# A09A9 confirmation-custody verifier

`scripts/verify_confirmation_custody_integration.py` is an independent,
offline regression verifier for the confirmation-to-`done` boundary. It imports
the current workspace modules and official synthetic fixture builders at
runtime, attests their resolved `__file__` paths and SHA-256 values, and never
starts ONI, Steam, a Bridge process, a Mod, or a real command queue.

## Cases

The default CLI runs exactly these four cases, each in a fresh
`TemporaryDirectory`:

1. `normal_commit_control` — publishes a legal confirming certificate, stages
   the real commit journal, runs real recovery twice, and checks one confirmed
   `done` owner, an intact root/session certificate, empty commit blockers, and
   no remaining confirming source.
2. `commit_waits_for_lifecycle_lock` — holds the real shared lifecycle lock in
   the parent, sends a same-script `--worker commit` child through a bounded
   ready/go/calling handshake, and samples the command tree, certificates, and
   commit journal at least every 20 ms for at least 0.3 seconds.
3. `recover_waits_for_lifecycle_lock` — repeats the same observation against
   the real `recover_staged_confirmed_outcomes` entry point.
4. `stale_source_replacement_preserved` — first proves a separately created B
   command with the same ID is valid and commits normally. It then hooks only
   `action_outcomes._command_locations`, returns the original A snapshot, and
   atomically replaces the confirming source with B after that scan. The safe
   expectation is that B remains at its original path/identity/bytes and A is
   not published to `done`; an approved custody conflict/unknown or
   `ActionOutcomeError` is acceptable, but an unknown result is not success.

The verifier is deliberately a diagnostic gate, not a production-fix claim. It
captures the protected baseline while the real lifecycle lock is held, before
the child receives `go`; it records only strict metadata for the held lock and
hashes every other protected command, journal, certificate, and session file.
The child protocol is strictly `ready -> calling -> result`, with bounded,
fully drained stdout/stderr, reader EOF/error/aliveness evidence, module-path
attestation, and a Temp-only lexical root boundary. A normal control runs
first; later cases are explicitly blocked if it fails, and the stale-source
case's independent B control must pass before its one-shot A-scan/B-replace
hook can start. Any missing case, zero-assertion case, cleanup error, worker
protocol/output fault, source drift, or incomplete final-state inventory fails
the report. A future production defect therefore remains an explicit failed
report with exit code 1; offline verifier success is not live admission.

## CLI and evidence

Run from the workspace root:

```powershell
python -B scripts/verify_confirmation_custody_integration.py
```

The default report is created with exclusive `open("x")` in a unique
`reports/a09a9-confirmation-custody-verifier-<UTC>-pid-<pid>-nonce-<nonce>/`
directory. `--report <path>` is available for an explicitly chosen, still
create-only report path. The internal `--worker commit|recover` entry point is
used only by the parent verifier; it requires an absolute temporary root,
validated marker, token, and the same script path. Its stdout/stderr are
bounded, its process has a 20-second post-release limit, and forced
termination or residual processes fail the gate.

The report records case names/counts, per-case assertion counts and fixture
allocation, source/test hashes before and after, parent and child module path
attestation, child PID/return code/handshake events, bounded output and reader
EOF/error/aliveness state, every protected-tree sample/change/error, lock
header evidence outside the held interval, cleanup results, and the normal/B
positive-control results. The six authoring hashes are retained as an
informational baseline comparison; the runtime gate compares actual
before/after snapshots and does not reject a future legitimate production fix
just because its SHA differs from this authoring baseline. Standard output is
only a short case summary and report path.

## Current verification boundary and rollback

For this authoring task, only in-memory `compile` and AST checks are allowed;
the verifier, fixture, worker, pytest, benchmark, Bridge, and game have not
been run. A future `status=passed` is still offline evidence only and does not
admit a live save or normal Mod configuration.

Rollback is limited to restoring these two files from the exact, hash-verified
preimage copies retained in the unique repair report directory:

- `scripts/verify_confirmation_custody_integration.py`
- `docs/a09a9-confirmation-custody-verifier-20260907.md`

The production modules, existing fixtures, old verifiers, original saves,
deployment files, queues, Mod/configuration, and network/proxy state are never
part of this write set.

## A09A9C1 boundary for this task

- 本步只改 `scripts/verify_confirmation_custody_integration.py` 的门禁判定逻辑，不扩展到任何生产模块或验证输入。
- `_gate` 一致消费 `_run_locked_worker` 的真实 `run_info` 结构（在 case
  `details.worker` 下保存）：`operation/command_id/launch_error/sample/cleanup/lock_header/lock_header_error/child_command`；
  聚合判定中的 `pid/returncode/reader/protocol/result` 均从其真实的
  `cleanup` 子结构读取，不再假设不存在的顶层 `worker.pid`。
- `_lock_case` 强化 `reader_eof/reader_errors/reader_alive/dropped_lines` 的双流精确映射校验；
  缺失、额外键、空值和类型替换不再被接受。
- normal 与 stale/B 分支的正控制与最终状态检查只基于实际 `final_state`/`b_control` 结构；
  仍保留现有恢复、重放、Hook 与 cleanup 约束，不执行生产运行。

## A09A9C1 final bounded status (2026-09-07)

C1 的门禁修复范围已封存为 `_gate` 及其纯 no-I/O 证据谓词：严格非布尔
整数计数、非空路径证据、必需 nullable 安全字段的显式键与 `None`、真实
`ready/calling/result` worker 事件、六模块路径/格式化 SHA-256 attestation、
以及与事件一致的 protocol summary。`reader_alive` 按正常 producer 的实际
`stdout_reader`/`stderr_reader` 键校验；producer、spawn/finish、case 执行、
fixtures 和生产模块均不在本步写范围。

本步的唯一接受证据是独占 `reports/a09a9c1-final-<UTC>-pid-<PID>-nonce-<nonce>/`
目录中的纯 AST/compile/in-memory synthetic suite JSON。该 suite 会绑定最终
source/doc 与六个保护源的独立 before/after SHA-256，并逐项覆盖历史 `_03`
负例、returncode/cleanup/reader/protocol/nullable 反例。即使 suite 报告
`passed`，它也只表示离线 gate 证据，不表示 verifier、真实 child、ONI、
Steam、Bridge、Mod、命令队列或 live admission 已执行。

本次 A09A9C1 pure acceptance 已完成：外层 `python -B` 进程实际退出码为 0，
full realistic positive 为 1/1，负例为 65/65 rejected，66/66 结果匹配，
gate exception 与 source/doc/protected drift 均为 0。可复核报告为本次独占
目录中的 `a09a9c1-final-pure-suite-retry-04.json`；该结果仍仅是 offline
gate evidence，不改变上面的 live-game 边界。

## A09A9C2a partial worker ownership and bounded cleanup

本阶段只修复 verifier 内部 worker 的 partial-resource custody；不改变
`_worker_protocol`、`_wait_for_worker_stage`、`_sample_while_locked`、case
body、`_lock_case`、`_gate`、fixture、生产 API、阈值、CLI、inventory 或
lifecycle-lock/header sampling。`_run_locked_worker` 在任何可失败 acquisition
之前创建唯一外层可达 mutable ledger，并把同一对象传给 marker/spawn/finish。

ledger 在 exclusive marker create 成功后立即记录 creation ownership，并在
open handle 上登记 marker identity；Popen 返回后先登记实际 handle 与三条
stream，再读取 pid 或构造 reader。每个 reader 先登记、再单独记录
start-attempted/start-returned/actually-started；构造失败、start 失败、ready/
baseline/go 失败都保留 primary error。go 只允许一次，write/flush 失败记录
uncertainty，禁止 replay。

cleanup 顺序固定为 stdin、owned process 的有界 pump/wait、同一 handle 的
bounded kill/wait、已存在 reader 的有界 drain/join、按 stream 独立关闭非活跃
pipe，最后按 recorded identity 与实际内容 hash 验证并删除自有 marker。碰撞、
缺失、替换、无法绑定或删除失败的 marker 不会被 unlink；活跃 reader 的 pipe
不会关闭。现有成功字段保持兼容，`reader_alive` 使用
`stdout_reader`/`stderr_reader`，并新增可序列化 ownership/ordered cleanup
evidence；任何 launch 或 cleanup failure 仍由既有 gate 拒绝。

本阶段只接受 source/doc 的 AST/compile 与注入 fake resource 的内存 harness
证据。harness 不导入 verifier 顶层、不运行真实 Popen/thread/pipe/marker/path、
fixture、pytest、Bridge、游戏或 Steam。fake 证据只能证明 ledger 分支、清理
顺序、一次性 go、序列化和有限 clock；不证明 Windows EOF/PID 退出、跨进程
锁、真实 child stream 或 live admission。rollback 仅使用本阶段独占报告目录
中经 SHA-256 复核的两份 preimage。

## A09A9C2a Luna source repair checkpoint (2026-09-07)

本次作者 atomic step 只修改 verifier 的 C2a ownership helpers、必要的
`_spawn_worker` reader-registration glue、`_finish_worker` cleanup glue，以及
精确恢复 `_worker_protocol` 到 accepted C1 preimage。exclusive marker 的文本
写句柄与后续 `rb` 读句柄现在分别登记，写入/关闭错误保留在 ledger；每次新
binding 只能在前一个 rb 已确认关闭后建立，并从 offset 0 读取。binding 同时
绑定 path `lstat` 与 opened-handle `fstat` 在读取前后的 identity、文件类型、
reparse 状态、size 和 `mtime_ns`，读取始终限制在 `MAX_CHILD_LINE * 16`，第一
份 partial 内容 hash/size/identity 不能被后续观察重新绑定，complete marker
还必须匹配 encoded payload hash。

reader 的 `thread`、`thread_started`、`is_alive`、EOF 和其他状态属性访问都
保留原始异常；unknown/alive reader 不会关闭其 pipe，剩余 owned resources
仍按既定顺序继续清理。protocol 函数不再修改传入 mapping，也不再在协议
summary 阶段越界初始化 reader error lists；这些 lists 由 ledger/finish scope
管理。实际工作区 source 前镜像与本次 source/doc hash readback 位于新的
`reports/a09a9c2a-luna-source-...` 目录。

截至本 checkpoint 只完成 source/doc 的静态修复、AST 范围检查和内存
`compile(bytes, filename, "exec")`；没有运行 verifier、fake suite、目标函数、
Popen/thread/pipe/marker、fixture、pytest、Bridge、Steam、游戏、Mod、队列或
网络。故本步是 static-only、unaccepted evidence，不证明资源安全、真实子进程
行为、生产 confirmation custody 或 live-game admission。主控仍需独立进行
static review，并由后续新 helper/新目录执行独立 fake acceptance。

## A09A9C2a four cleanup blockers repair (2026-09-07)

本次后续 atomic step 仍只触及 reader 状态交接、`_PipeReader.start` 和
`_finish_worker` cleanup glue。缺失 reader 只有在 `process_owned` 以及
`reader_registered`、`reader_start_attempted`、`reader_start_returned`、
`reader_started` 都由 pre-ensure ledger 明确记录为 False 时，才会独立关闭
已经登记的 owned pipe；ledger 缺字段、ownership 或 start 状态 unknown 时不
关闭。缺失 reader 本身仍产生不完整 EOF/cleanup evidence，不能被当作 EOF。

thread start failure 现在保留原始 start exception，并按 `is_alive`、`ident`
观察顺序保留 secondary exception；任一观察失败都会把 started state 置为
unknown，不能再用 ident 回退覆盖 unknown。该状态、原始/secondary error 会
贯穿 reader ledger、ownership summary 和 cleanup error history；alive 或未被
证明 inactive 的 reader pipe 不关闭。

所有最终 reader 属性在 cleanup `errors/phase/cleanup_error` seal 前各守护读取
一次，异常先加入本次 attempt errors，再生成 immutable-like result snapshot；
seal 后只使用 snapshot，不再执行 fallible reader getter。重复 cleanup 会按顺序
累计历史 errors 与 forced-kill history，同时单独输出本次 attempt 的 errors、
forced-kill、residual 和 outcome；后续 attempt 成功不能把先前 failure 变成
clean verifier evidence。

本 step 仅允许 source/doc 的内存 compile、AST/文本范围和 hash readback；不运行
verifier、目标函数、fake suite、Popen/thread/pipe/marker、fixture、pytest、
Bridge、Steam、游戏、Mod、队列或网络。故本节仍是 static-only、unaccepted
evidence；不能证明 Windows EOF/PID、真实 child resource cleanup、生产
confirmation custody 或 live-game admission。rollback 只使用本次独占目录中
经 SHA-256 复核的两份 preimage。

## A09A9C2a lifecycle provenance and lookup-error accounting follow-up (2026-09-07)

本次窄修复把 reader 生命周期的“原有明确状态”和 `_ensure_worker_ledger`
补出的默认值分开持久记录。`_finish_worker` 和 `_ensure_worker_ledger` 在
补全四个 lifecycle map 之前写入 `reader_lifecycle_explicit` provenance；只有
在补全前已经存在且为布尔值的
`reader_registered`、`reader_start_attempted`、`reader_start_returned`、
`reader_started` 才能参与 `safe_to_close` 判定。缺失或非法字段会永久保持
unknown，重复 cleanup 不会因为上一次 `setdefault(False)` 而升级成 known
False。

因此，缺失状态的反例必须保持为：`process_owned=True`，对应 pipe 已登记，
对应 `reader=None`，且四个 lifecycle map key 都缺失；该 reader 的
`lifecycle_evidence` 仍为 unknown、`safe_to_close=False`，pipe 不会在第二次
cleanup 被关闭。相反，实际 `_spawn_worker` 在 reader 构造失败前已从完整
ledger 明确登记未启动状态时，允许独立关闭 owned pipe；其明确状态是
`process_owned=True`，并且四项 lifecycle 值均为 `False`（不是
`process_owned=False`）。

四类历史 reader lookup error list 使用持久游标而不是每次只看“当前列表非
空”。游标从零开始，所以 `_spawn_worker` 在首次 cleanup 前已经写入的 lookup
error 不会被跳过；最终 reader getter 在本次 cleanup 新增的 error 会被本次
attempt 计入，后续 cleanup 只计入游标之后的新 error。这样累计
`cleanup_errors`/首个 `cleanup_error` 的原始顺序、verifier failure 和
`forced_kill_history` 都保留，而 `cleanup_attempt_errors` 与
`cleanup_attempt_outcome` 只反映尚未计入或本次新观察到的错误。final getter
snapshot、原始/secondary reader start error、marker A/C/D 重试算法和其它
资源算法不变。

本 follow-up 仍只接受 source/doc 的 AST、内存 `compile`、文本范围及 SHA-256
证据；不运行 verifier、fake suite、目标函数、Popen/thread/pipe/marker、
fixture、pytest、Bridge、Steam、游戏、Mod、队列或网络。状态仍为
`static-only-unaccepted`，不构成真实资源清理、production confirmation
custody 或 live-game admission 证明。

## A09A9C2a marker retry state repair (2026-09-07)

本次 `A09A9-C2A-MARKER-REPAIR-02` 只修复 marker cleanup 在重复调用时丢失
已观察 unlink 结果的问题。`_new_worker_ledger` 新增持久的 unlink effect/history、
最后一次 `lstat` presence state/error 和 `present_after` 字段；默认状态是
`unattempted`/`unobserved`，不会把缺失字段当作成功删除。`_finish_worker` 的
marker 分支用 `os.lstat` 做 no-follow presence 观察，只有 `FileNotFoundError`
表示 absence，其他异常保持 `unknown` 并进入 cleanup errors。unlink 返回成功后
记录 `removed` 及其历史，并保留 fresh absence 证据；后续 cleanup 对已知 removed
marker 只重新观察路径，不 reopen、read、rebind 或再次 unlink，因此不会重置
已关闭的 read-handle 证据。

如果路径被重新创建为普通文件、符号链接或 dangling link，`lstat` 都记录为
`present`，cleanup 报告该路径并保留它；不会采用新对象或删除它。unlink 异常的
effect 保持 `unknown`，重试前先观察路径，无法确认时不重新绑定。历史 cleanup
errors 与 forced-kill history 继续按原顺序累计。`_capture_marker_binding`、C1、
protocol、timeouts、case bodies、CLI、inventory、生产模块和六个保护源均未改动。

本步仅完成 source/doc 的 AST、内存 `compile`、统一范围 diff 和 SHA-256 静态
证据；没有运行 verifier、fake helper、目标函数、Popen/thread/pipe/marker、
fixture、pytest、Bridge、Steam、游戏、Mod、队列或网络。状态仍为
`static-only-unaccepted`，后续 fullfake、inert-real、production 和 live-game
acceptance 仍需独立流程。
