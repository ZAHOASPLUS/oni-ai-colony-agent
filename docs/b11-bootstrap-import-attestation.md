# B11 bootstrap import attestation

日期：2026-09-06（Asia/Shanghai）。本门是 fixture-only 离线证据，不是正式 production wrapper 验收，也不授予 live-game admission。

> B11-T correction（2026-09-06）：本文件初版关于“迟延导入用例已测试”的说法已撤回。只读 `unittest` loader 当时实际发现 6 个测试；`test_main_only_undeclared_import_fails_in_post_main_audit` 位于 `if __name__ == "__main__": unittest.main()` 之后且不属于测试类，因此没有被 discovery 执行。请以 [B11-T corrected evidence](b11t-attestation-evidence.md) 和新命名 evidence 为准。

## B11-T correction and retest

修正后的 fixture-only 专项把迟延导入用例移入 `BootstrapImportAttestationTests`，并将报告 status 绑定到真实 `unittest.TestResult`：failure/error/expected-failure/unexpected-success 或源码漂移为 `failed`；必需用例 skip、未运行或集合不完整为 `incomplete`；只有完整 required IDs、无坏结果且 protected source hashes 前后一致时才是 `passed`。标准模块入口和直接脚本入口都实际执行 7 tests。

## 实施结果

`scripts/run_live_runtime_bootstrap_verification.ps1` 的固定 production dependency list 已补入并锁定以下三个实际 import 来源：

- `agent/action_policy.py`
- `agent/command_custody.py`
- `agent/control_packet.py`

`scripts/run_live_runtime_bootstrap_verification_worker.py` 现在在 verifier 完成导入、manager binding 校验之后、调用 `verifier.main()` 之前执行现有严格 `audit_loaded_project_modules`。原有 post-main audit 保留，因此导入时和执行中延迟发生的未知 project source 都会分别 fail-closed。

没有增加 `AdditionalSourceFile`、fixture manifest、report override 或自动信任开关；production worker 仍只接受 wrapper 传入的固定、受审查 source set。

## Closure evidence

只读导入 production verifier（未调用 `main()`）观察到 27 个 project `.py` 来源。与固定清单比较后，新增的三个来源均实际加载，且 `unlockedSources=[]`。专项测试记录 fixed dependency count 为 26；另含 verifier/manager 本身的固定输入。

证据文件：[test-evidence.json](../reports/b11-bootstrap-import-attestation/test-evidence.json)

## 独立 fixture matrix

所有 source、report、marker 和 run-scoped pycache 均在新的系统 Temp 根中；未启动 production verifier main、ONI、Steam 或 Bridge。

| fixture | exit | 关键证据 |
| --- | ---: | --- |
| complete attested delayed import | 0 | main、delayed marker、report、attestation 均存在；pycache 为空 |
| import-time undeclared import | 1 | import marker 存在，main marker/report/attestation 不存在；pre-main 拒绝 |
| main-only undeclared import | 1 | B11-T复测：main、delayed marker、report 存在，attestation 不存在；post-main 拒绝 |
| missing required loaded source | 1 | main/report 存在，attestation 不存在；required-source 拒绝 |
| wrong manager binding | 1 | main/report/attestation 均不存在；binding 拒绝 |
| spec.origin mismatch | 1 | main/report/attestation 均不存在；spec 校验拒绝 |
| production fixed closure | 0 | 27 loaded、26 fixed dependencies、0 unlocked source |

初版记录的 `6 tests / 1.092s / exit 0 / OK` 不包含上述迟延导入用例，不能作为完整覆盖证据。B11-T复测的精确入口、test IDs、exit、耗时、stdout/stderr 和 marker 布尔值见 [b11t-attestation-evidence.md](b11t-attestation-evidence.md) 与 [corrected evidence directory](../reports/b11t-attestation-evidence/)。

## 既有 runner 回归

命令 `python -B -m unittest scripts.test_live_runtime_bootstrap_runner` 正常退出：`34 tests / 104.862s / exit 0 / OK`。完整观测日志及本轮 source/canonical hash 记录在：

[runner-regression-20260906.json](../reports/b11-bootstrap-import-attestation/runner-regression-20260906.json)

## Hash audit

| 文件 | before SHA-256 | after SHA-256 |
| --- | --- | --- |
| `scripts/run_live_runtime_bootstrap_verification.ps1` | `FDBF11BD6D227B32385AF9F0EBA9D3E076E12E12703CE9C4FE4115FE621D8069` | `BF404B1F15291B112C0DF103DA95788DB13A43BA49010EC803D7EA696064B81F` |
| `scripts/run_live_runtime_bootstrap_verification_worker.py` | `97DA43FA6EB2B689E0532CEED8026447DF6DDD449B47D9BEDFD01949AAEA5E75` | `DC2C5FCC992CC630F59EA890CD06138EC25AD9B8E4174ACA69F15E6DB7DC65A9` |
| `scripts/test_live_runtime_bootstrap_runner.py` | `1DCF1765E4A65E3606EC53895B85FD3C57598EB78D97CBAEB99E16708691B0A4` | unchanged |
| `reports/live-runtime-bootstrap-verification.json` | `167B00D57A8259A24030A2046A00FD6265E4A6FA7F2B269C33A4DCFCDC89F5AB` | unchanged |

新增测试脚本 `scripts/test_bootstrap_import_attestation.py` 的 after hash 为 `861AD59131E11508B055DF82700E616AB29956778BB25DCDA41637247CE1C4A4`。本任务未修改 `agent/action_policy.py`、`agent/command_custody.py` 或 `agent/control_packet.py`；它们仅作为固定 closure 输入被读取。

## 收口与边界

- B11 fixture 子进程均已退出；最终 matching process census 无 ONI、Steam、Bridge、worker 或 verifier 进程。
- 本次显式编译产生的 worker `.pyc` 已按精确路径清理；run-scoped fixture pycache 均为空，历史 `__pycache__` 未清理。
- 新增 evidence/report 位于 `reports/b11-bootstrap-import-attestation/`；canonical report 未写入、未发布、未覆盖。
- formal wrapper 两轮、504 scenario/4032 assertion、source-handle publication、真实存档/Mod/Bridge、G0 与 live-game acceptance 均仍未证实。
