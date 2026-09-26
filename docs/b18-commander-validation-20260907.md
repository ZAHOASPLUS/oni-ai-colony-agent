# B18 commander acceptance — 2026-09-07

Accepted as a bounded offline change, not live admission or indefinite survival.
The sole production diff is a `_API_SECRET_RE.search(text) is not None` guard
around the unchanged substitution in `agent/strategy.py::_safe_text`.
Normalization, regex, substitution, slicing and exception order are unchanged.
No changes were made to state quality, field contracts, Bridge, or old verifiers.

## Evidence

| Run | Result | Report under workspace | SHA256 |
| --- | --- | --- | --- |
| B18 pure AST differential | Exit 0; 24/24 cases, 27 assertions, 3 real-regex positive controls, 3 AST gates, stable hashes | `reports/b18-safe-text-fastpath-20260907T063615851Z-pid43252-d00fc8e0242a/report.json` | `CE024588AFCD55A3C46EAB5687552158B6EC627063F361A2311D3DBAC3588CF7` |
| Original strategy regression | Exit 0; 14/14 cases; identical report bytes to prior baseline | `reports/ONI_B18_STRATEGY_REGRESSION_20260907_01/reports/strategy-verification.json` | `926298E53787AB12D11D32437B1597ED99BF52CD8BA785FAFD168789A067B21A` |
| Dedicated fresh replay | Exit 0; five input/output classes exact, 15/15 sanity gates, 24/24 benchmark gates | `reports/b16-strategy-hotpath-20260907/runs/ONI_B18_ASTRA_REVIEW_20260907_POSTPATCH_01/post-patch.json` | `9C462D5194A295A1E0998976760005651A3DEBC51F15C4B29BD982B84E9FE865` |
| Unmodified official Bridge verifier | Exit 0; 15/15 scenarios, 399 assertions | `reports/ONI_B18_BRIDGE_INTEGRATION_20260907_01.json` | `14571B3A1730AC2A5A5A18BBF6FB34C8F54EBA49A9B6BD28184F9CE5572CFCC0` |

Dedicated replay: 10,000 records, 7.7361039 seconds, 135.171875 MiB peak,
381 memory samples, startup binding verified, health/trust ready, final strategy
sequence 10,000, complete archive, no sequence gaps. Child returned 0, stderr
empty, handles closed, no residual child, temporary roots removed.

Official report generated at `2026-09-07T14:55:13.216+08:00`:

- Original-scope replay: 2.712 seconds / 127.598 MiB (5-second gate).
- Startup-bound deferred replay: 2.752 seconds / 132.125 MiB (5-second gate).
- Startup-bound live-initial replay: 7.897 seconds / 137.590 MiB (8-second gate).
- All retain their original 150 MiB gate and 20-second hard timeout. The term
  live-initial identifies the offline Bridge mode; no actual game ran.

The official elapsed headroom is only 0.103 seconds. This single pass is not a
robust load margin, repeated-run guarantee, or proof that the guard alone caused
the timing difference. Do not rerun unchanged sources merely to select a pass.
The prior official 8.082-second failure is retained unchanged at
`reports/ONI_A09A6_BRIDGE_INTEGRATION_20260907_01.json`, SHA256
`7E18752204DDCFCEA1466BBCC641B668E9C6EC55201FE6ACD224F7F26F27D5AB`.
The older canonical `reports/bridge-reliability.json` also remains unchanged,
SHA256 `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D`.

## Freeze and run boundaries

114 Python/PowerShell sources in agent/bridge/scripts were unchanged across
root validation, `2026-09-07T06:53:53.0568351Z` to
`2026-09-07T06:55:48.2456592Z`:

- Before: `reports/ONI_B18_VALIDATION_20260907_01-sources-before.json`, SHA256
  `329AD90431AB754247B4AB775AF7B890B7F5210715CA1394704861D75CCD3A0E`.
- After: `reports/ONI_B18_VALIDATION_20260907_01-sources-after.json`, SHA256
  `6F6A5C0814F649F983118C239D311D61D41B67EFCBCAC3E8A46FA640502C0C14`.
- Strategy: `6D92E3BE31CB0141D046D4757C78581C1306AA28BC8DFEA292C451FBB6B85885`.
- B18 verifier: `3714230F89F934F26A0D107960009764FC38126B4B01192C638DB21E0A67BA82`.
- State quality: `49C0A2C6B894E5EB519B8115B8F244EF14A58AA1D7EB912C291B21CA777FDB6D`.
- Field contracts: `259A23FAD48D870501093E192B42CA4D41587538FD12C3D123F2914446F18837`.

The original strategy verifier was imported unchanged. Only its output-only
ROOT global was rebound to the new exclusive report root; its case functions,
production imports and assertions were not patched. Dedicated baseline ID is
`ONI_B16_SOL_HOTPATH_20260907_BASELINE_01`; the full original CLI was used for
the official Bridge verifier with only a new `--report` path.

The B18 task `01a07a93-894b-7521-a739-708a2328611b` is terminal. A commander
steering message crossed its original completion; the follow-up stopped before
any duplicate differential run. Its extra directory
`reports/b18-safe-text-fastpath-20260907T065150710Z-pid14004-8b6175bc690f`
contains staging only, no report; it is retained and is not verification evidence.
Do not restart that worker merely to compress the verifier's line count.

Post-run relevant process census is empty. There are no remaining
`oni-bridge-v2-`, `oni-b16-sanity-`, or `oni-b16-benchmark-` Temp roots.
The longstanding unrelated MihomoMonitor pythonw process was classified by its
script path and left untouched. No proxy, network, or model setting changed.
Original cloud saves/previews, normal Mod/config and real queues were not read
or mutated: their current hashes and queue emptiness are therefore UNVERIFIED.
The fourteen earlier approval staging evidence directories remain retained.

Rollback, only if necessary after verifying no intervening source edits: use
the exact preimage `reports/b18-safe-text-fastpath-20260907T063615851Z-pid43252-d00fc8e0242a/preimage/strategy.py`,
SHA256 `2D70076900DA0B21CC77A9267888C0CF5C4C69E7143F30B0A0F0B77B063D9AF5`,
to reverse only the guard; preserve all reports and A09A8 changes.

## Next boundary

Continue bounded confirmation-stage custody audit and its independent negative
fixture before implementation. Formal current-source bootstrap two-round
verification, all-consumer strict inventory, unknown recovery, real clone/G0,
complete telemetry and renewable multi-fault survival evidence remain open.
The goal remains active and the live gate remains closed.
