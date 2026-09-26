# ONI offline independent review — 2026-09-06

Status: **integration failed; no live admission**. The full bootstrap/stabilize/
expand/space/sustain objective remains active and unachieved. This record does
not replace the resource-loop acceptance requirements in the active architecture
plan or `survival-action-gap-audit.md`.

## Execution and authority

The latest user route is Astra command/review and Luna Max implementation in
new Codex tasks. Task `01a0736f-2d20-7002-add7-cafdb6a6fa61`, titled
“ONI A09A-2：共享归属模块验收”, emitted real assistant/tool events, then its turn
`01a0736f-2ea3-71e2-8e0d-556a37f43094` failed with:

> Your workspace is out of credits. Add credits to continue.

This is a terminal failure, not a live wait or a startup-stall candidate. Its
shared custody source stayed at the baseline hash below; neither the proposed
core verifier nor its completion document was created. No credit reset,
purchase, model fallback, or duplicate dispatch was performed. A nonblocking
user question asks whether the main task may temporarily implement the two
newly localized repairs. Absent that permission or restored Luna execution,
code implementation cannot be represented as continuing.

The 05:34 +08 transport preflight reported stable=true over 120 seconds,
networkDisconnectCount=0 and freshTransportProcessCount=0. No network settings
were changed. All actions in this record were offline diagnostics or report
writes; no original save or normal Mod/configuration was modified. This is
**not** a fresh before/after hash audit of real game assets.

## Fresh normal-exit evidence

The certificate-specific process exited 0 at approximately 05:17 +08. Three
compatibility processes exited 0 at 05:33–05:34. The eight-verifier matrix ran
05:36:23–05:38:59 and its parent exited **1** because Bridge reliability failed.
All associated stderr logs are zero bytes. A final process census found no
ONI game or matching verification/Bridge Python process.

| Scope | Observed result | Evidence |
| --- | --- | --- |
| Survival certificate | 8 groups, 1,282 assertions, 36 priority pairs / 180 variants; exit 0 | `reports/a08b2-review-20260906.stdout.json` |
| Decision frame | 17 cases; exit 0 | `reports/a08b2-frame-review-20260906.stdout.json` |
| Approval frame binding | 18 cases; exit 0 | `reports/approval-frame-binding.json`, `reports/a08b2-approval-review-20260906.stdout.log` |
| Strategy | 14 cases; exit 0 | `reports/strategy-verification.json`, `reports/a08b2-strategy-review-20260906.stdout.log` |
| Command pipeline gate | 18 cases; exit 0 | `reports/matrix-20260906-command_pipeline_gate.stdout.log` |
| Control packet | 6 cases; exit 0 | `reports/matrix-20260906-control_packet.stdout.log` |
| Approval transactions | 12 cases; exit 0 | `reports/matrix-20260906-approval_transactions.stdout.log` |
| Immutable frames | 9 cases; exit 0 | `reports/matrix-20260906-immutable_decision_frames.stdout.log` |
| Command safety | 8 cases; exit 0 | `reports/matrix-20260906-command_safety.stdout.log` |
| Colony plan, static mode | 5 cases; exit 0 | `reports/matrix-20260906-colony_plan.stdout.log` |
| Bridge reliability | **14 passed / 1 failed**, 397 reached assertions; exit 1 | `reports/bridge-reliability.json`, `reports/matrix-20260906-bridge_reliability.stdout.log` |
| Automation | 13 groups including 362,880 alert permutations and 11 replay scenarios; exit 0; 123.953 seconds | `reports/matrix-20260906-automation.stdout.log` |

These are independent reruns of the current code, not acceptance of the
unfinished shared-custody implementation. Existing tests do not prove the new
custody race/recovery requirements. Passing the certificate's 1,282 assertions
did not cover the actual mixed incident collection used by Bridge.

## Reproduced certificate/Bridge regression

Failure: `control_packet_session_and_policy_isolation` reports
`Bridge did not publish the unique current candidate`.

The same existing test was invoked separately without its surrounding temporary
directory cleanup. Preserved synthetic root:

`C:\Users\user\AppData\Local\Temp\oni-review-candidate-tmwlqlck`

Only test fixtures exist there; never point production tools at it. In its
`candidate` directory, the fresh artifacts establish:

- The low-oxygen episode has seven consecutive samples, snapshot sequence
  1–7, event sequence 2–8, and a 30.006-second duration.
- One pending `set_paused` command exists, and all executor-side stages are
  empty. No command was approved or executed.
- The frame certificate has status `blocked`, reason
  `open_incident_alert_invalid`, active id `low_oxygen`, and no selected id.
- The control packet emits `observe_request` for
  `mutation_pending_selection:cmd-81cabac9b7fcb951a5959025dc6632d6`, not an
  action candidate.
- The full incident projection contains 21 legitimate rule incidents, including
  seven non-alert ids: `idle_duplicants`, `low_food`, `missing_manual_generator`,
  `missing_outhouse`, `missing_oxygen_diffuser`, `no_active_research`, and
  `stressed_duplicants`. They are inactive in this fixture.

Root cause is directly visible in the production call sites:

1. `bridge/oni_bridge.py` passes `runtime.incidents.open_incidents()` to advice
   arbitration, so this healthy mixed-history fixture selects low oxygen.
2. `agent/decision.py` passes the **complete** incident collection to certificate
   construction.
3. `agent/rules.py::_normalize_survival_incidents` rejects every incident id
   outside the 14 canonical survival-alert ids, before the reducer applies its
   relevance filtering. The legitimate non-alert history therefore blocks the
   certificate.

The shared reducer fixed the earlier inactive-higher-witness acceptance bug,
but did not establish that producer and verifier receive equivalent domains.
Correct repair needs a closed distinction between known non-alert rule
incidents and authorizing survival alerts; simply dropping all unknown ids,
deleting ordinary observations, or weakening higher-priority witness checks
is not acceptable. Tests must use `evaluate_signals` + `IncidentTracker` and
the full Bridge publication path, not alert-only handcrafted projections.

## New bootstrap dependency attestation gap

A read-only isolated/no-bytecode Python process imported the production
verifier and enumerated loaded project module paths; **verifier main was not
called**. Comparison with the wrapper's fixed production dependency list found
27 loaded project sources and these three missing held sources:

- `agent/action_policy.py`
- `agent/command_custody.py`
- `agent/control_packet.py`

The production worker's `audit_loaded_project_modules` rejects a loaded project
source outside the held set. It currently runs after verifier main. Thus the
current wrapper cannot establish a successful final attestation even if its
two test rounds pass. This is source-backed preflight evidence, **not** a fresh
formal-wrapper execution or a fresh moved-Bridge failure.

Do not use `AdditionalSourceFile`, a fixture manifest, a report-path override,
or weakened attestation to obtain a production pass. The production closure
must be updated and regression-tested, ideally with a pre-main closure check
that preserves the existing post-main check. Formal two-round verification
remains unrun against this snapshot.

The canonical bootstrap report remains the old hash
`167B00D57A8259A24030A2046A00FD6265E4A6FA7F2B269C33A4DCFCDC89F5AB`.
The later failed worker and both prior preserved test roots remain untouched;
neither the old pass nor the recent targeted pass overrides missing fresh
production acceptance.

## Source and evidence hashes

Relevant source hashes were unchanged across this review. SHA256:

| Source | Hash |
| --- | --- |
| `agent/rules.py` | `1FC6B4EA99BF422742FE8B500C7D8C517A908D33F92671A248A13F73361F8317` |
| `agent/decision.py` | `1BBDDBFAEA6C0F05D18C04A5673DFB129E5C6CDA3BB96EC7042A4D69A4CC8CBF` |
| `agent/approval_receipt.py` | `27266A6FFF338E45B8C4497FC22AE21A0A6BE62457AA3A26D3C9134ADE27CFE3` |
| `agent/command_custody.py` | `3133A0A68FE609CCF2F9C37E0C61A6450F91972BDFEBFDAD72B34481B551EA1A` |
| `agent/command_queue.py` | `4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1` |
| `bridge/oni_bridge.py` | `0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C` |
| `scripts/run_live_runtime_bootstrap_verification.ps1` | `FDBF11BD6D227B32385AF9F0EBA9D3E076E12E12703CE9C4FE4115FE621D8069` |
| `scripts/verify_survival_certificate.py` | `DF4009B0DA2C44EC8B5D14ADCF7A4B83ADFFCCA096E522960C0735CA9EB01370` |
| `scripts/verify_bridge_reliability.py` | `FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337` |

| Evidence | Hash |
| --- | --- |
| `a08b2-review-20260906.stdout.json` | `4359238B61CAE093F4B595A3236A79CFEB5DC2CF7B3F06DB92F1D7F9A80853DC` |
| `a08b2-frame-review-20260906.stdout.json` | `B68246BB78E8434EFD5E3212C635362458076B189D605E8E5CE4C638EAC4DCCB` |
| `approval-frame-binding.json` | `305D562B1B558FC55F7235AC54A53D7A3B7FAE4B00D6D31AEB2BA5B50F2ABDAE` |
| `strategy-verification.json` | `926298E53787AB12D11D32437B1597ED99BF52CD8BA785FAFD168789A067B21A` |
| `bridge-reliability.json` | `BB93AB6BDD3A834C55C280BD311A5F212A97FF5E43CADA454A066591ACFB8785` |
| `matrix-20260906-automation.stdout.log` | `2DB44466DB1B8B832607FD2E717E5DE7F777808A8782A1E8344C4F39B98D7B62` |

Last completed behavioral action: `BRIDGE_CANDIDATE_REPRO_20260906`.
Last documentation action: `OFFLINE_REVIEW_RECORD_20260906`.
There is no live-game action id, pending game dispatch, or verification process
left to wait on. Do not restart the failed Luna task until execution is
available; do not silently switch the requested implementation route.

## Next admission sequence

1. Repair/test the mixed-incident certificate boundary; independently rerun
   the actual Bridge candidate test plus certificate/approval regressions.
2. Finish A09A-2 core custody verification, then integrate queue/approval/
   publishers under a separately bounded packet; core success alone is not
   integration success. See `a09a2-resume-20260906.md`.
3. Correct the fixed bootstrap import closure and freeze all writers before
   formal two-round wrapper acceptance. Preserve exact 504/4032 and scenario
   manifest checks; no production acceptance overrides.
4. Complete bounded offers and admitted-sample source history, then the stable
   full regression matrix. Self-consistent certificates remain non-authenticating.
5. Only after every relevant gate passes, prepare a new live checkpoint with
   original/test-clone hashes, fresh observed process/Mod/queue state, exact G0
   forward/inverse action witnesses, restoration and shutdown evidence.
6. Continue all-world/domain telemetry, functional building capabilities,
   renewable resource/dependency/fault-domain proofs, and all five survival
   phases. None of these are satisfied by the control-plane tests above.
