# B12 Bridge replay performance evidence — 2026-09-06

## 结论

本专项保持 failed，未证明 5 ms 的 Windows 内存查询是 8.0 s 失败的主要原因，因此没有修改验证 harness，也没有修改 Bridge 生产业务代码。

在同一类 10,000 行 startup-bound fixture 上，三种离线测量各跑 3 次：

| 测量 | live 初始 replay（秒） | live 峰值工作集（MiB） | 结论 |
|---|---:|---:|---|
| 原 measure（5 ms GetProcessMemoryInfo 轮询） | 8.300 / 8.149 / 8.281 | 134.961 / 134.789 / 135.090 | 3/3 超过 8.0 s |
| 候选 measure（communicate(timeout=remaining)，完成后取 lifetime peak） | 8.452 / 8.406 / 8.277 | 135.133 / 135.277 / 135.098 | 3/3 超过 8.0 s |
| 等价 no-poll Popen + communicate() | 8.191 / 8.189 / 8.120 | 未采样 | 3/3 超过 8.0 s，不能作为内存上限证据 |

因此不能把 8.0 s 放宽、跳过 startup binding、跳过解析/输出/安全校验，或把 no-poll 的结果伪装成通过。当前真实 verifier 的结果为 15 scenarios / 377 assertions：14 个 scenario 通过，唯一失败是 large_replay_budget 的 startup-bound live initial replay 为 8.349 s。

## 安全边界

- PERF_LINE_COUNT 仍为 10,000。
- MAX_LIVE_INITIAL_REPLAY_SECONDS 仍为 8.0 s。
- MAX_PEAK_WORKING_SET 仍为 150 MiB。
- 原 harness 仍在超时路径 kill 子进程，并检查 return code；错误消息继续保留 stderr 或 stdout 尾部。
- 候选采样器只在内存中实验，没有写入工作区；它保留了 20 s 硬超时、kill、return-code fail-closed 和完成后 lifetime peak 检查，但因性能结果仍失败而未采用。
- Bridge 的 replay_initial_records()、write_outputs()、run_once() 路径没有改动；没有启动 ONI/Klei/Steam，没有真实 Klei 写入，没有读取原始云存档，没有修改正常 Mod/config，也没有改变 VPN/Clash/Mihomo/TUN。

## 文件与 hash

| 文件 | before SHA-256 | after SHA-256 |
|---|---|---|
| scripts/verify_bridge_reliability.py | FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337 | FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337 |
| bridge/oni_bridge.py | 0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C | 0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C |
| scripts/test_bootstrap_import_attestation.py（B11-T 独占） | 38231876A4A51EBA074974631D009C79CE044B03CE881ED3B16F799D3518A1A4 | 38231876A4A51EBA074974631D009C79CE044B03CE881ED3B16F799D3518A1A4 |

运行前的 canonical Bridge failure report 已保存在 reports/b12-bridge-replay-performance/bridge-reliability-before.json；其 hash 为 4F740961051FCD5A37A545FB39DF4482F3533F6EC3C7B15D2AB8A09992AEBB37。fresh verifier 生成的 canonical report hash 为 11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D，仍为 failed。

## 离线测试结果

| 命令 | exit | 计数 | 状态 |
|---|---:|---:|---|
| python -B scripts/verify_bridge_reliability.py | 1 | 15 scenarios / 377 assertions；14 pass / 1 fail | failed（仅 large_replay_budget） |
| python -B scripts/verify_survival_mixed_incidents.py | 0 | 5 cases / 160 assertions | passed |
| python -B scripts/verify_survival_certificate.py | 0 | 8 cases / 1,282 assertions | passed |
| python -B scripts/verify_decision_frame.py | 0 | 17 / 17 cases | passed |
| python -B scripts/verify_bridge_evidence_continuity.py | 0 | 6 passed cases | passed |
| python -B scripts/verify_command_pipeline_gate.py | 0 | 18 passed cases | passed |
| python -B scripts/verify_control_packet.py | 0 | 6 passed cases | passed |
| python -B scripts/verify_approval_frame_binding.py | 0 | 18 passed cases | passed |
| python -B scripts/verify_approval_transactions.py | 0 | 12 passed cases | passed |
| python -B scripts/verify_confirmation_transactions.py | 0 | 4 passed cases | passed |
| python -B scripts/verify_immutable_decision_frames.py | 0 | 9 passed cases | passed |
| python -B scripts/verify_action_outcomes.py | 0 | 9 passed cases | passed |
| python -B scripts/verify_command_safety.py | 0 | 8 passed cases | passed |

逐命令日志保存在 reports/b12-bridge-replay-performance/ 下。实验和 verifier 使用的 TemporaryDirectory 均已清理；复现前缀目录数为 0，残留的 ONI/Klei/Steam/Bridge/verifier helper 进程数为 0。

## 下一单一动作

下一轮只做离线 profile，定位 startup-bound live 路径中 ReplayState.process、strategy evaluation 和 snapshot trust 检查的实际 CPU 成本；保持 8.0 s、150 MiB、20 s 和所有 fail-closed 门槛不变，完成前不进入实机测试。
