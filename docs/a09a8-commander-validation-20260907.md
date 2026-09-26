# A09A8 commander acceptance — 2026-09-07

**Bounded offline repair accepted; live admission remains closed.**
The old-source failure baseline is retained unchanged in
`a09a7-plan-custody-baseline-20260907.md`. All nine new runs below exited 0;
93/93 cases passed at the frozen sources. This count excludes the deliberately
failed old-source baseline and is not a claim about every release gate.

## Implemented and independently reviewed

- Plan create/advance/pause/resume/abort/recover use the shared OS lifecycle
  lock. The retired plan lock is rejected, never stale-deleted.
- Pending publication is create-only. Quarantine uses durable custody with
  complete expected-envelope, raw-byte, file-identity and mtime checks.
- The additive observation API accepts matching legacy pretty JSON without
  relaxing old callers. Observation refuses recovery evidence instead of
  silently performing recovery.
- Post-publication context loss preserves a paused/blocked revision, then
  attempts exact quarantine. Unknown isolation preserves evidence and requires
  recovery; the plan never silently deletes and retries the same command ID.
- Abort refuses approving, approved, executing, and confirming leaves.

Root review corrected an ignored returned revision, an observation/recovery
side-effect mismatch, weak test before/after comparisons, verifier integrity
gates, and the unknown-isolation test double's signature before functional
acceptance. Existing custody and legacy verifiers were not weakened or edited.

## Frozen execution results

| Suite | Cases | Explicit assertion count | New evidence path under `reports/` |
| --- | ---: | ---: | --- |
| Observed envelope | 14/14 | 69 | `a09a8-observed-envelope-core-20260907T062259.266138Z-pid33876-ff54ca90c62e/report.json` |
| Original Plan custody verifier | 4/4 | 42 | `a09a7-plan-custody-verifier-20260907T062330034219Z-32412-540c3c79bdc2/report.json` |
| Publication recovery | 7/7 | 62 | `a09a8-plan-publication-recovery-20260907T062330196182Z-13088-a606be663edc/report.json` |
| Original custody core | 8/8 | 77 | `a09a8-core-regression-20260907T062411297498Z-35876-89a0ea9fe502/command-custody-core.json` |
| Original Colony Plan static | 5/5 | Not reported | `ONI_A09A8_COLONY_STATIC_20260907_01.json` |
| Original command pipeline | 18/18 | Not reported | `ONI_A09A8_PIPELINE_20260907_01.json` |
| Approval boundary | 4/4 | 34 | `a09a6-boundary-verifier-20260907T062457619014Z-8808-eb34a8455e/report.json` |
| Approval custody | 3/3 | 80 | `a09a3-approval-custody-integration-20260907T062457623054Z-34056-c53910cb89/report.json` |
| Original approval regressions | 30/30 | Not reported | `a09a4-approval-regressions-20260907T062457.614508Z-42928-afa4e5f6db3b5013/report.json` |

The six suites that report assertion totals contain 364 passing assertions.
Do not invent totals for the other three suites.

The held-lock Plan cases captured 18 advance samples and 16 abort samples:
zero changed samples, zero sample errors, no early outcomes, then normal child
exit 0 after release. Old source had changed in every sampled window. The
fresh pretty-JSON positive control and stale A/B preservation both passed.
Publication recovery produced the expected consecutive three revisions;
unknown isolation preserved the command captured before the injected fault.
All 14 observed-envelope fixtures had nonzero body assertions.

The core runner was imported from its exact workspace module and its
`REPORT_DIR` rebound only in the invoking process to a newly created unique
directory. Its original verifier source and historical default reports were
not edited. Original Plan used `--report`; pipeline used `--output` with new
paths. No `--live` flag was used. All remaining runners allocated unique paths.

## Hashes

Production:

- `agent/command_custody.py`: `6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763`
- `agent/colony_plan.py`: `0E5B3D174699A1CC54F42CA749838D766176069C4B5F43AD2AD20B0D8D047ADD`

New tests:

- `scripts/verify_observed_pending_envelope.py`: `10749E71E6604A609BC4FAAAE6480AB110A8E8DD63E02FB868458B86591054EA`
- `scripts/verify_colony_plan_publication_recovery.py`: `66BED55A94876E6BEE8708D25CF744BF5870DF687B1AE2E80A1AFB6E187B7A50`

Reports, in the same table order:

1. `31671E0D45DA23C7529AD4CA378F36700CB850A1B9760A414886F14C073EC9CC`
2. `EF2B2ACA117A8E33AE03D1EA81B68FA6B5D7AEEF07C35D4D9CA342DA053EFFC0`
3. `3DA9C64131BC3A3CE01B531EFF54F9B4E512D63B7CEBB2356256627809BFE6DF`
4. `A8E810B1BC2AABDE33F6D6C819A1FBBE2CBAEF22C9A53442017E14F7088B5054`
5. `88DEA89E8DC0EE95A8CDBA81BDAF5E5286323D351B7E0CC6AA02D79600588CD3`
6. `8079480AB6413978F0E171197B70A320AA9BB7FA6AF14B3A350594624E6DEA83`
7. `D893B7A71164C2032EA7EE3B3733CDC4350ED66A277D81C8B2B585EAD6EE4316`
8. `CEC2366B2E7F4562D402BA7729A8308FE47FBCD8B6CFA3855D752727F24A946D`
9. `3530361CB0F9CD87D197FDF0FDA0A028457CAABA8E584E3049C322CE1649754A`

All 113 `.py`/`.ps1` files under `agent`, `bridge`, and `scripts` were identical
before and after these runs, with no added or missing source. Manifests:
`ONI_A09A8_VALIDATION_20260907_01-sources-before.json` (SHA-256
`4BA937EE3783B36B7704783C41DCD9F2E13F4CE3AE1B8F6503501FE7486B7EB4`)
and `ONI_A09A8_VALIDATION_20260907_01-sources-after.json`.

## Cleanup and admission boundary

All owned test fixture roots reported removed. Relevant final process census
at 2026-09-07 14:26:05 +08:00 found no game/Steam/Bridge/verifier/spawn process;
the core's separate forbidden-process census was empty. No original save,
preview, normal Mod/configuration, or real queue was used or mutated, and none
is asserted to have a current hash inventory or an empty state.

The 30-case approval suite retained seven new OS-private `oni-approval-*`
staging directories, by the already existing conservative cleanup policy.
There were seven before and fourteen after. Exact paths and the before/after
set difference are in the after-manifest. Nothing was deleted. These retained
staging artifacts are distinct from successfully removed fixture roots.

Still open: raw malformed/ambiguous Plan inventory, other lifecycle consumers
(especially confirming → done), uncertain approval recovery availability,
official Bridge replay performance (last unchanged-source run 8.082 seconds
against 8.0), fixed-source formal bootstrap rounds, and all required live
admission evidence. The previously passing B16 micro/dedicated run does not
supersede that official Bridge failure. No new Bridge performance run occurred
here. The extra B18 performance delegation was rejected by the subagent thread
limit and never started; strategy/state-quality/field-contract sources remain
unchanged. The overall survival goal remains active.
