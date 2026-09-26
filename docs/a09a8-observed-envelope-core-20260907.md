# A09A8 observed pending-envelope core

日期：2026-09-07（UTC）  
工作区：`C:\Users\user\Documents\ONI`

## 变更范围

本次只写入以下三个任务文件：

- `agent/command_custody.py`
- `scripts/verify_observed_pending_envelope.py`
- 本文档

`agent/command_custody.py` 的改前精确 preimage 已备份到：

`reports/a09a8-observed-envelope-core-20260907T060431527Z-pid23436-8d73fdc7f1b1/preimage/command_custody.py`

改前 core SHA-256：

`FAACF7C429D3711E4BFCC33FF64E5247FF7725B61570BC72A66D4D02FD413DD1`

当前静态文件 SHA-256：

- `agent/command_custody.py`：`6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763`
- `scripts/verify_observed_pending_envelope.py`：`10749E71E6604A609BC4FAAAE6480AB110A8E8DD63E02FB868458B86591054EA`

## Core contract

新增 frozen dataclass `PendingEnvelopeObservation`，只有 `path`、`payload`、`identity`、`mtime_ns` 四个字段，没有 mutable command 字段。新增 public `observe_pending_envelope(...)` 在 lifecycle lock 内先拒绝已有 recovery evidence，然后做完整 protected-stage stable scan、唯一 command-id owner proof、strict JSON/schema/status 校验，并以 `_json_bytes(dict(expected_source))` 对比 strict-parsed actual 的 canonical bytes。观测返回 scan 得到的真实 raw bytes、inode identity 和 mtime。

`transition_pending_envelope(...)` 及其 locked helper 新增可选 `expected_observation`。启用该通道时，`expected_source` 不能省略；观测 path 必须 canonical，payload、identity、mtime 必须和本次严格 scan 完全相同，且 expected source canonical bytes 必须等于本次 strict-parsed source canonical bytes。source 已消失时，即使目标已有相同 final payload，也拒绝 stale observation，不把旧幂等分支当作成功。所有新判断均在 recovery payload、journal、terminal rename 之前；原有无 observation 分支仍使用 exact canonical-byte 比较。

## Verifier design and boundary

`scripts/verify_observed_pending_envelope.py` 是独立临时 fixture runner，不修改旧 test，不启动 game/Steam/Bridge，不联网，不调用 pytest、subprocess 或子进程。每个 case 都用独立 `TemporaryDirectory`，并检查临时目录已移除；异常不会被标成 skip，而会计为 fail。fixture 执行前核对实际 import path，并冻结 core 与 verifier 的 before/after hashes。报告目录名带 UTC、pid 和 nonce/x，报告包含实际 import path、两者 hashes、逐 assertion 事件及非零断言计数。

覆盖用例：fresh pretty/compact observation 成功；A 观察后 B 替换拒绝；相同 bytes 新 identity 拒绝；语义相同但 whitespace 改变拒绝；观察时 expected A 而实际为 B 拒绝；错误 path 拒绝；observation 缺 expected_source 拒绝；source 消失时 stale observation 拒绝；无 observation 的 legacy compact 成功且 legacy pretty 仍按旧 exact-byte 规则拒绝；duplicate owner、non-finite JSON、malformed JSON 拒绝；已有 recovery evidence 时 observe 拒绝且 source/evidence bytes、identity、mtime 不变。

本任务按主指挥要求只做静态核对，未运行 verifier、fixture、pytest 或任何真实队列/游戏操作；最终运行与 acceptance 由 root 在冻结各 writer 源后统一执行。
