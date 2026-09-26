# B11-T corrected bootstrap attestation evidence

日期：2026-09-06（Asia/Shanghai）。这是 fixture-only、离线的测试与报告修复；不启动 production verifier `main()`，不启动 ONI、Steam 或 Bridge，不修改真实存档、Mod、policy、网络或 production wrapper/worker/manager/verifier。

## 纠错结论

B11 初版的“迟延导入用例已测试”说法不成立：只读 `unittest` loader 实际只发现 6 个 test IDs；该方法写在 `if __name__ == "__main__": unittest.main()` 之后，也不属于 `BootstrapImportAttestationTests` 类。旧 evidence 原始 bytes 已先复制并核对 SHA-256，保存在 [original-test-evidence-20260906.json](../reports/b11t-attestation-evidence/original-test-evidence-20260906.json)。

B11-T 将该用例移入真实测试类，并使用 module-level `load_tests` 包装公开 `unittest.TestSuite`。suite 完成后依据实际 `unittest.TestResult` 的 failures/errors/skips/expected-failures/unexpected-successes、实际执行 IDs、required 集合和 protected source hashes 生成报告。未运行、skip、failure、error 或源码漂移均不能生成 `status=passed`。

## 两个正常入口

| 入口 | 实际结果 | evidence |
| --- | --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 python -B -m unittest scripts.test_bootstrap_import_attestation` | `7 tests / 1.261s / exit 0 / OK`；7/7 required/executed，0 failure/error/skip/missing，`status=passed` | [module-entrypoint-evidence-20260906.json](../reports/b11t-attestation-evidence/module-entrypoint-evidence-20260906.json)，SHA-256 `94B145F1F69A527732D08884CF4D2076858098F59BADBF54558BD5BC20E28295` |
| `PYTHONDONTWRITEBYTECODE=1 python -B scripts/test_bootstrap_import_attestation.py` | `7 tests / 1.258s / exit 0 / OK`；7/7 required/executed，0 failure/error/skip/missing，`status=passed` | [script-entrypoint-evidence-20260906.json](../reports/b11t-attestation-evidence/script-entrypoint-evidence-20260906.json)，SHA-256 `922AB6097C7365CAF4237F8FBD5056D1C53BDF82ECA4DF3A1FC9D9A1DBA0DFA3` |

模块入口的 required IDs 为：

```text
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_complete_attested_fixture_executes_main_and_passes
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_import_time_undeclared_import_fails_before_main_marker
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_main_only_undeclared_import_fails_in_post_main_audit
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_missing_required_loaded_source_is_rejected
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_production_fixed_closure_covers_read_only_import_closure
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_spec_origin_mismatch_is_rejected_before_main
scripts.test_bootstrap_import_attestation.BootstrapImportAttestationTests.test_wrong_manager_binding_is_rejected_before_main
```

直接脚本 evidence 记录了同一 7 个方法的 `__main__.BootstrapImportAttestationTests...` IDs；两入口的集合和计数均来自实际 loader/run，而不是源码字符串匹配。

## Fixture matrix

主 evidence：[test-evidence.json](../reports/b11-bootstrap-import-attestation/test-evidence.json)。每个 case 的 `unittestStatus` 都为 `passed`，表示该测试本身通过；case 的 `exitCode` 是被测 fixture worker 的真实进程退出码。

| fixture | worker exit | main marker | delayed marker | report | attestation | 关键断言 |
| --- | ---: | --- | --- | --- | --- | --- |
| `delayed_attested` | 0 | true | true | true | true | 完整声明的迟延导入可通过 |
| `import_undeclared` | 1 | false | false | false | false | import-time 未声明 source 被拒绝 |
| `delayed_undeclared` | 1 | true | true | true | false | stderr 含 `loaded project module is outside the held source set`，post-main audit 拒绝 |
| `plain` + missing required | 1 | true | false | true | false | stderr 含 `required project sources were not loaded` |
| `plain` + wrong manager | 1 | false | false | false | false | manager binding 拒绝 |
| `spec_mismatch` | 1 | false | false | false | false | `spec.origin` 与 `__file__` 不一致拒绝 |
| production fixed closure | 0 | n/a | n/a | n/a | n/a | 27 loaded、26 fixed dependencies、0 unlocked source |

六个 fixture Temp 根均在测试结束时不存在；run-scoped pycache 为空。delayed case 的 marker 与报告存在、attestation 不存在，证明它确实进入 `main()` 并在 post-main 审计失败，而非被 pre-main 分支替代。

## 独立 reporting 负测

命令 `PYTHONDONTWRITEBYTECODE=1 python -B -m unittest scripts.test_bootstrap_attestation_reporting`：`1 test / 0.147s / exit 0 / OK`。测试在新的 Temp 根中通过真实 unittest 子进程执行两条边界：

- deliberate assertion failure：子进程 `exit 1`，生成的 report `status=failed`，failure 列表非空；
- required-case skip：子进程 `exit 1`，生成的 report `status=incomplete`，skip 列表非空，即使 `collectionComplete=true` 也不能 passed。

两条子进程的 protected source hashes 都前后一致；外层负测在 hash unchanged 时仍验证 failure/incomplete，未手工改 JSON，也未只调用 report builder。汇总证据：[negative-reporting-evidence.json](../reports/b11t-attestation-evidence/negative-reporting-evidence.json)，SHA-256 `B4620BB0409F3C751DC4F5135D4268CF1F88B4D4BA33711463F0778FF5AA8A76`。

## Protected hash audit

主专项 report 对以下路径在本次运行内记录了相同的 before/after SHA-256：

| path | SHA-256 |
| --- | --- |
| `scripts/run_live_runtime_bootstrap_verification.ps1` | `BF404B1F15291B112C0DF103DA95788DB13A43BA49010EC803D7EA696064B81F` |
| `scripts/run_live_runtime_bootstrap_verification_worker.py` | `DC2C5FCC992CC630F59EA890CD06138EC25AD9B8E4174ACA69F15E6DB7DC65A9` |
| `scripts/manage_live_runtime_bootstrap.py` | `28904E8C91F7615F6701B97ABE6D87FD232C8350E266CC1616E12B8E12A77158` |
| `scripts/verify_live_runtime_bootstrap.py` | `08AE7E58DFC551EA3B5830FBDDED33CA691FE5E42FA371AC00D416B9960B751A` |
| `scripts/test_live_runtime_bootstrap_runner.py` | `1DCF1765E4A65E3606EC53895B85FD3C57598EB78D97CBAEB99E16708691B0A4` |
| `reports/live-runtime-bootstrap-verification.json` | `167B00D57A8259A24030A2046A00FD6265E4A6FA7F2B269C33A4DCFCDC89F5AB` |

上述六个 protected hash 均由本次主专项 evidence 的 before/after 字段记录为相同的连续 64-hex 值。测试脚本本身是本次有意修改的 test-only 文件；新增 reporting 负测也是 test-only 文件。

## 边界

本专项通过只证明离线 fixture import-attestation 与 reporting gate 的证据链修复。它不等于 formal wrapper 两轮、504 scenario/4032 assertion、source-handle publication、真实存档/Mod/Bridge、G0 或 live-game acceptance；这些仍是未证实状态。
