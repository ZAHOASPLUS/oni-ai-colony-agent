# A09A9 confirmation compatibility contract — source review only

Action ONI_A09A9_COMPATIBILITY_RECORD_20260907_01, 2026-09-07T07:44Z.
Read-only Luna audit a09a9_legacy_contract_check plus root verification of
action_outcomes.py 1147-1178, 1258-1343, 1403-1508 and the old verifier's
189-260. No fixture/import/test/production mutation. This note constrains a
future production repair; it does not authorize one before the current
test-only verifier repair and root baseline review are finished.

## Preserve semantic compatibility, bind physical ownership separately

Legacy exact confirming+done recovery currently validates the done payload
with confirmed_command_commit_blockers and compares it with the *full*
_confirmed_command_payload reconstructed from the confirming source and
published outcome using _strict_json_equal. This is recursive JSON semantic
equality, not raw-file equality; whitespace and object key order are irrelevant,
list order and boolean type remain meaningful. Do not silently strengthen this
to raw equality and call the resulting compatibility break a safety fix.

Important root clarification of the audit proposal: the existing confirming
source helper accepts either executed_pending_snapshot or confirmed. Only the
done result must be confirmed. The audit's suggested requirement that *both*
sources be confirmed is stricter than current behavior and is not accepted as
the default contract. Preserve the full existing transformation/validation,
or separately justify and test any intentional semantics change.

Only a canonical pair with exactly one confirming and one done owner, no third
location, a valid published confirmation, no done blockers, and full expected
payload equality may collapse. Cleanup must bind the original source path,
raw bytes/hash, file identity and mtime observed before decisions; revalidate
before deleting. Ambiguous, conflicting, replaced or unprovable evidence stays
in place and fails closed. Common publish/pending disposition APIs must retain
their existing all-duplicate rejection. Isolate any legacy exception in a
dedicated same-lock confirming adapter, not a global preflight relaxation.

## Preserve certificate and recovery semantics

Stage requires validated, semantically identical root/session action-outcomes
documents. Existing commit marker fields are schema, outcomes_hash, session_id,
root_certificate, session_certificate, staged_at, transaction_hash. It is a
certificate publication binding, not proof of physical queue ownership.
Confirmed payload retains the full outcome, confirmed_at and witness result
fields, not merely status=confirmed. For approval-bundle actions, authority
must still be established before archival, including idempotent re-entry;
missing/conflicting authority must not be bypassed by new custody APIs.

The core's current journal operation/stage mapping is pending-only. Do not
encode confirming operations under a pending disposition name. If extending
its operation model, retain durable exact original/final evidence and explicit
recovery phases, and preserve unknown material. Recover/commit entry points
must hold the shared lifecycle boundary across the decision, not only unlink.
Source scan evidence must include full inventory and canonical path/status,
raw bytes, identity and mtime; certificate intent/witness hashes alone cannot
establish those facts. Final success requires one canonical confirmed done
owner and physical absence of the original confirming source.

## Existing fixture hook is implementation-coupled

verify_confirmation_transactions._leave_legacy_duplicate monkeypatches
action_outcomes._rename_with_retry: it copies the already rewritten confirming
file to done, then raises SimulatedCrash. A design which no longer rewrites
the source or uses that rename helper can make this injection unreachable,
even while preserving the actual legacy-recovery contract.

Do not retain unsafe source replacement just to satisfy this private hook, and
do not turn "injection never fired" into pass or silently skip the old case.
Before such a production refactor, either demonstrate a safe compatible hook
boundary, or separately freeze and minimally update the fixture construction
to produce the same verified legacy pair directly, with before/after field,
ownership and certificate equivalence evidence. That would require an explicit
new test-file scope and root review; it is NOT authorized to the current
two-file A09A9 verifier-repair worker. Preserve old reports as historical.

## Reviewed source hashes

- agent/action_outcomes.py: C0FAB71E21F1E49CBE76927BF6CF4E16A7C0179BEDB290CC1EA11B4EA56060D7
- agent/command_custody.py: 6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763
- scripts/verify_confirmation_transactions.py: 157E191A5030BF0D64742AAA5575F7E845EDBAEFA7C5E4F338A3F442A7B09AC9

Unresolved until implementation and isolated tests: durable confirming operation
schema, exact certificate/ownership binding timing, cross-language custody,
crash recovery and safe compatibility-fixture migration. No live admission.
Rollback for this documentation-only action is removing only this new note
after verifying it has no intervening edits; no production rollback is implied.
