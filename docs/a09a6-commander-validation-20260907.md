# A09A6 commander validation — 2026-09-07

Status: approval-entry offline regressions accepted at the hashes below;
integrated Bridge reliability FAILED. No live admission, normal-save/config
verification, or indefinite-colony-survival result is claimed.

## Frozen production and verifier

- `scripts/commit_command_approval.py`: `FC69BFE53956B981FF5ADB2CF2109B19D7BE564947F5E7EA32C5DA6DA68E3582`
- `scripts/approve-command.ps1`: `8C045BAE06230A3E2EBEC1E1DD6DF56DDB771E9D053BFB8C7EEB0059FE29DBB6`
- `scripts/verify_approval_boundary_safety.py`: `2B6EA95E7802FF3DF1AC35EE810C9A8D5B998076825C9378DEAB6FBB0D4DD132`
- `scripts/verify_approval_custody_integration.py`: `127419F22441B184F8D654F3F6E619AC7CE01342FC8ADED6A6E7DF149EE113D1`
- `scripts/verify_approval_adapter_regressions.py`: `A88E677191A745284D3B073CD8ACEA600534156F173B49AB83F83F60DFE21C3A`

Both A09A6 writers delivered and stopped before execution. The commander
reviewed the production diff and verifier, correcting the moved-sentinel
reference and missing baseline guard for Case A before testing. No fixture or
threshold was weakened. Original cloud saves, normal Mod/configuration, and
real queues were neither read nor mutated by this round.

## Fresh approval evidence

| action_id | Exit and result | Report SHA-256 |
| --- | --- | --- |
| `ONI_A09A6_BOUNDARY_20260907_01` | 0; 4/4 cases, 34/34 assertions | `C0CE79FD79F2A4B053E1321BE6A1254515BAFD1456125F4BA0B2D9915FE94A55` |
| `ONI_A09A6_CUSTODY_20260907_01` | 0; 3/3 cases, 80/80 assertions | `B8754140DCBEB42A5F90822EFC496A1289E49C3269CA7AB17C47A1C1CA9078B4` |
| `ONI_A09A6_LEGACY_20260907_01` | 0; original 12 transaction + 18 frame cases passed | `1BD1DA7EC95276AD3E377851095677CC1F37596421F1C50268D902CE17C3D82F` |

Exact reports, relative to the workspace root:

- `reports/a09a6-boundary-verifier-20260907T051758504268Z-44916-e81ef49e63/report.json`
- `reports/a09a3-approval-custody-integration-20260907T051821006571Z-37660-177cf56915/report.json`
- `reports/a09a4-approval-regressions-20260907T051855.810532Z-45732-bc252b3e5639cd6d/report.json`

Boundary run: 2026-09-07T05:17:58.504Z to 05:17:59.487Z. Ten actual
held-lock samples had no queue stage/root changes. Child PID 44864 completed
after lock release (672 ms commit duration), exited 0, was not terminated,
and had no residual. Foreign recovery directory identity and sentinel bytes
were unchanged; unknown outcome retained the owned candidate evidence.
All four temporary roots were removed after owned-child cleanup.

Custody run: 05:18:21.006Z to 05:18:22.383Z. Source drift was false,
normal child PID 46480 exited 0 without forced termination, and all temporary
roots were removed. Assertions vary with actual sample count; this run had
8 + 65 + 7 = 80, not the earlier diagnostic run's 77.

Legacy run: 05:18:55.810Z to 05:19:08.733Z. All 30 fixtures were removed;
source hashes were identical before/after, without manifest errors. The six
previous frame failures now pass. The root's subsequent targeted process
census found no game, Steam, Bridge, approval worker, or multiprocessing-spawn
residual. This is not a claim that real queues are empty.

Fourteen pure verifier controls also passed: twelve failure variants were
rejected (status error, explicit failure, zero assertions, false assertion,
missing/failed cleanup, missing child, nonzero child exit, forced termination,
unknown residual, shutdown error, close errors), a positive control passed,
and an in-process module-path mismatch was detected. These controls created
no fixture and are not an independently launched shadow-import experiment.

## Deliberately retained evidence / unresolved recovery

The PowerShell entry stages outside `Bridge/commands` in atomically allocated
system temporary directories. Its cleanup intentionally retains those files
because it cannot prove object identity strongly enough to delete safely.
Seven directories were created by the legacy run and remain under
`C:/Users/user/AppData/Local/Temp/`:

- `oni-approval-43n34ytz.lp0`
- `oni-approval-hkhmqj4w.2wq`
- `oni-approval-p1xvufsc.kp1`
- `oni-approval-rmvnlmmi.jog`
- `oni-approval-teusxcro.4y2`
- `oni-approval-ytrtpggo.0im`
- `oni-approval-zuwguazk.44p`

No deletion was attempted. The old Spark zero-byte shadow-module diagnostic
residue is also retained, as documented in the A09A6 recovery packet.
Do not claim all system temporary material has been cleaned. Empty
`approval-recovery` after a recovered failure can still block later strict
inventory: safe automatic recovery availability remains unresolved. These
tests do not prove complete hostile-ancestor replacement protection.

## Integrated Bridge failure

Action `ONI_A09A6_BRIDGE_INTEGRATION_20260907_01` exited 1. It ran the unchanged
official 15-case verifier with a new explicit report path:

`reports/ONI_A09A6_BRIDGE_INTEGRATION_20260907_01.json`

SHA-256: `7E18752204DDCFCEA1466BBCC641B668E9C6EC55201FE6ACD224F7F26F27D5AB`.
Fourteen cases passed; 377 assertions were reached before/including failure.
`large_replay_budget` failed with:

> 10,000-line startup-bound live initial replay took 8.082s

The 8.0-second threshold remains unchanged. The case duration was 14.519s
including its other work; that is not the timed replay duration. Do not infer
the remaining memory/output gates passed after this early assertion failure.
The previous dedicated B16 7.7866534s result remains valid only for its own
run and did not demonstrate robust integrated timing margin.

All 110 Python/PowerShell files under `agent`, `bridge`, and `scripts` had
identical before/after hashes. The complete manifests are:

- `reports/ONI_A09A6_BRIDGE_INTEGRATION_20260907_01-sources-before.json`
- `reports/ONI_A09A6_BRIDGE_INTEGRATION_20260907_01-sources-after.json`

No relevant verifier/Bridge child remained after normal runner exit; no
`oni-bridge-v2-*` temporary root remained. Canonical historical Bridge failure
report stayed `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D`;
canonical transaction and frame reports also retained their known hashes.

## Next bounded work

Two independent read-only Luna Max audits are in progress: Colony Plan queue
writer/custody integration and a next strategy hot-path equivalence candidate.
Neither can edit or run fixtures. Commander acceptance is still required
before a separately scoped implementation. No timing rerun without an
evidence-backed change is scheduled; no gate relaxation is permitted.

The full objective still requires remaining consumer integration, durable
recovery, official pipeline/control-packet/strategy/bootstrap acceptance,
verified named clone and immutable inputs, reversible G0 native observation,
then colony oxygen/food/sanitation/water/power/temperature and renewable
fault-tolerant loops through expansion and space. These offline passes are
not substitutes for that evidence.
