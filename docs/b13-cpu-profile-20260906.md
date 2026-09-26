# B13 startup-bound CPU profile — 2026-09-06

Status: diagnostic completed. The 8.0-second performance gate is not passed, and this evidence does not authorize a real-game run.

## Scope and safety boundary

This was an offline CPU-profile run only. It did not launch ONI, Klei, Steam, Bridge, or the verifier as a live helper; it did not read original cloud saves; it did not change the normal Mod/configuration, command queues, VPN/Clash/Mihomo/TUN, or production code. The full `verify_bridge_reliability.py` suite was not rerun.

The transport preflight reported `stable=true`, zero NetworkProfile disconnects, zero fresh transport-helper processes, and one Codex process with 20 established HTTPS connections. The exact process census used exact executable names and exact Bridge/verifier script-path tokens, rather than a name substring; it found zero forbidden processes before profiling (522 total processes) and at final cleanup (519 total processes).

The current canonical failure report was recorded before and after as SHA-256 `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D`. The protected B12 copy remains SHA-256 `4F740961051FCD5A37A545FB39DF4482F3533F6EC3C7B15D2AB8A09992AEBB37`.

## Fixture and measurements

The new fixture contained exactly 10,000 lines: one trusted startup-bound `session_started` envelope and 9,999 `state_snapshot` records for `session-performance`. Each run generated a fresh copy under an isolated `TemporaryDirectory`; the input size was 52,172,381 bytes. Only the existing verifier's fixture constructors were imported; its `main()` and test cases were not invoked.

The uninstrumented controls used current timestamps. The cProfile groups used a 4.9-second future timestamp, within the existing 5.0-second future-skew tolerance, because profiling overhead otherwise crossed the 15-second freshness window before all records were processed. The integrated profile still reached 20.076384 seconds and became stale near the end; this limitation is retained in the evidence rather than being treated as a pass.

| Group | Wall clock | Coverage/result |
| --- | ---: | --- |
| `replay_initial_records` + `write_outputs` cProfile | 20.076384 s | 10,000 `process` calls, 10,000 trust checks, 6,971 strategy calls; final `latest_seq=10000`, cursor complete, output health `stale` because instrumentation crossed freshness |
| Isolated `ReplayState.process` cProfile | 19.443448 s | 10,000 records processed, 9,999 trust checks, 7,108 strategy calls, no startup-integrity blockers |
| Isolated `strategy.evaluate_strategy` cProfile | 16.046226 s | 9,999/9,999 snapshots evaluated |
| Unprofiled wall control 1 | 8.181556 s | `latest_seq=10000`, health `ok`, startup verified, 10,000 archived lines |
| Unprofiled wall control 2 | 8.301547 s | `latest_seq=10000`, health `ok`, startup verified, 10,000 archived lines |
| Unprofiled wall control 3 | 8.318628 s | `latest_seq=10000`, health `ok`, startup verified, 10,000 archived lines |

All three unprofiled controls exceeded the unchanged 8.0-second budget by 0.181556–0.318628 seconds. Their mean was 8.267244 seconds and their range was 0.137072 seconds. All were below 20 seconds, but this run did not measure peak working set and did not exercise subprocess kill-on-timeout behavior.

## Hotspot result

The complete bounded top-30 cumulative and top-30 self-time views for every profile are in `reports/b13-cpu-profile-20260906/profile-results.json`; the binary cProfile artifacts are retained beside it. The most useful inclusive path and self-time excerpts are:

| Evidence | Calls | Self time | Cumulative time | Interpretation |
| --- | ---: | ---: | ---: | --- |
| `bridge/oni_bridge.py:328` `ReplayState.process` (integrated) | 10,000 | 0.136580 s | 19.114752 s | Dispatcher-inclusive path accounts for 95.484% of profile self-time when measured as cumulative descendants |
| `bridge/oni_bridge.py:2249` `replay_initial_records` (integrated) | 1 | 0.015645 s | 19.933880 s | Replay loop and archive path dominate; the function's own body is not the cost center |
| `agent/strategy.py:1760` `evaluate_strategy` (direct profile) | 9,999 | 0.597840 s | 15.997132 s | Full direct strategy workload; 99.694% of that profile's self-time is beneath this call |
| `agent/state_quality.py:316` `snapshot_trust_blockers` (integrated) | 10,000 | 0.085015 s | 0.323653 s | The named blocker function is modest by itself; its repeated quality helpers are larger |
| `bridge/oni_bridge.py:2313` `write_outputs` (integrated) | 1 | 0.000184 s | 0.134651 s | Output publication is not the dominant path in this fixture |

The integrated top self-time functions were `dict.get` (1.748624 s, 8.735%), `isinstance` (1.219477 s, 6.092%), `copy.deepcopy` (1.209704 s, 6.043%), `field_contracts.condition_declaration_blockers` (0.750019 s, 3.747%), regex `Pattern.sub` (0.722709 s, 3.610%), `state_quality.field_quality` (0.667085 s, 3.332%), `strategy._quality_status` (0.598989 s, 2.992%), `strategy.validate_strategy` (0.541264 s, 2.704%), `state_quality.safe_get` (0.539404 s, 2.694%), JSON `encoder.iterencode` (0.490607 s, 2.451%), `strategy.evaluate_strategy` (0.439872 s, 2.197%), and `incidents._update_one` (0.439489 s, 2.195%).

Module self-time segments support the same conclusion:

- Integrated: `agent/strategy.py` 3.543788 s (17.702%), `agent/state_quality.py` 2.299310 s (11.486%), JSON serialization 0.800675 s (4.000%), and `bridge/oni_bridge.py` 0.266371 s (1.331%).
- Isolated `ReplayState.process`: strategy 3.617282 s (18.604%), state-quality 2.343142 s (12.051%), JSON 0.237913 s (1.224%).
- Direct strategy: strategy 4.424301 s (27.572%), state-quality 2.229352 s (13.893%), JSON 0.327567 s (2.041%). `copy.deepcopy` alone consumed 1.654551 s (10.311%) in this direct profile; repeated `dict.get`, regex substitution, type checks, `field_quality`, `_quality_status`, `_safe_text`, `_read`, and `validate_strategy` follow.

This fresh profile confirms the earlier hotspot hypothesis at the business path level: repeated strategy/quality/contract helper work is the material CPU path. It does not justify changing thresholds or code in B13.

## System-load interpretation and gate decision

The controls were serial, with one Python worker and 16 logical CPUs. A Windows-native sampler collected 29–30 samples per control. Whole-system CPU was:

| Run | CPU min / average / max | Memory load | Available physical memory |
| --- | ---: | ---: | ---: |
| 1 | 16.29% / 22.56% / 32.12% | 81–82% | 5,727.7–5,843.4 MiB |
| 2 | 15.15% / 21.40% / 35.38% | 82–83% | 5,467.4–5,715.2 MiB |
| 3 | 15.84% / 21.29% / 28.18% | 82–83% | 5,487.8–5,775.4 MiB |

There is no evidence of whole-system CPU saturation, although aggregate CPU does not rule out a single-core or process-specific contention effect. The narrow 0.137072-second control range and the direct 9,999-snapshot strategy profile make a business hot path more credible than a system-load-only explanation. Peak working set was intentionally not measured by this in-process profiler, so the 150-MiB memory gate remains unverified.

The unchanged gate contract remains 8.0 seconds, 150 MiB peak working set, 20 seconds hard timeout, kill-on-timeout, and fail-closed return-code/stderr handling. The B13 conclusion is therefore: diagnostic complete, 8-second gate not passed, no live-game authorization.

## Hashes and cleanup

Before and after hashes are duplicated in `reports/b13-cpu-profile-20260906/verify.json`. The production Bridge, strategy, state-quality module, reliability verifier, B11-T attestation test, canonical B12 failure report, and protected B12 backup were byte-identical. The temporary profile runner was removed after use. No `oni-b13-*` temporary directory, profile child, ONI/Klei/Steam process, Bridge process, or verifier helper remained.

Retained artifacts:

- `reports/b13-cpu-profile-20260906/verify.json`
- `reports/b13-cpu-profile-20260906/profile-results.json`
- `reports/b13-cpu-profile-20260906/integrated-replay-write_outputs.prof`
- `reports/b13-cpu-profile-20260906/replaystate-process.prof`
- `reports/b13-cpu-profile-20260906/strategy-evaluate_strategy.prof`

Next single action: run one bounded offline optimization experiment against the confirmed strategy/state-quality/contract-helper path, preserving the existing 8.0-second, 150-MiB, 20-second, kill-on-timeout, and fail-closed gates; do not enter the real game.
