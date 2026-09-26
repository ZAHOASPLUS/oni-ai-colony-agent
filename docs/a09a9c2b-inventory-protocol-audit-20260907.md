# A09A9 C2b inventory/protocol audit

Action `A09A9-C2B-AUDIT-SEAL-01`, 2026-09-07. This is a read-only audit
record, not an implementation contract, dispatch request, or execution permit.
No verifier, fixture, production module, child process, game, queue, gate, C1,
or C2a path was executed or changed.

## Evidence boundary

- Immutable preimage:
  `reports/a09a9c1-gate-20260907T081324Z-pid-41960-nonce-/preimage_verify_confirmation_custody_integration.py`
  SHA256 `F627E61AE937EA7B8815D5138E671AF8BC0D47DB3D8F440E57CA569AA5A96DFB`.
- Review note:
  `docs/a09a9-repaired-verifier-review-20260907.md`
  SHA256 `A82C2B74DAFA278D883F1168DD33F774953FDCEB9C11C2F8966B228ED0C02607`.

## Implemented and missing

`_tree_records` already rejects reparse points, non-directory roots, and
unsupported child types. `_stable_file_record` binds identity, mtime, size,
and bytes. `_protected_snapshot` emits sorted `records` and a digest, and
`_sample_while_locked` records the before-go baseline fields.

`_command_locations` still ignores every non-`.json` entry and accepts any JSON
dictionary whose `id` matches the requested target. C2b inventory must inspect
every entry in every `COMMAND_DIRS` stage, reject non-JSON material, malformed
objects, reparse/non-regular files, wrong schema, invalid IDs, and any payload
ID whose canonical filename is not exactly `<id>.json`. A valid nonmatching ID
is fully validated, preserved as inventory evidence, and excluded from the
requested target list/count; it is not deleted or treated as an error merely
because it does not match. The bounded ID form
`[A-Za-z0-9][A-Za-z0-9._-]{0,159}` is only an audit suggestion. Root must fresh
confirm the actual producer validator before enforcing it. The producer emits
`oni.command.v3`; done records retain core intent fields and add confirmation,
result, and acknowledgement fields, so exact-key equality is unsafe.

`_protected_snapshot` should use `lexists` before treating certificate paths as
absent; otherwise a dangling symlink can evade `_stable_file_record`. Baseline
strict-structure validation (nonempty records, allowed kinds, kind-specific
identity/hash fields, and digest consistency) is missing. Root must decide the
later validation point; this audit does not silently alter a gate.

`_worker_protocol` checks stage order, calling operation, result ID, result-list
type, ready attestation presence/path equality, and overflow. It lacks complete
operation/ID binding for ready/calling/result, exact three-mapping enforcement,
and full attestation shape/hash checks. `_worker_main` currently emits ready
without operation/ID, calling without ID, and result without operation. The
minimal producer-compatible event contract is:

- ready: `stage`, worker `operation`, marker `command_id`, and the six-entry
  `module_attestation` map;
- calling: `stage`, matching `operation`, matching `command_id`;
- result: `stage`, matching `operation`, matching `command_id`, and the API's
  list result.

The full attestation map need not be duplicated on calling/result; requiring a
digest echo there would be an explicit protocol extension. Format/path checks
do not prove an attestation SHA256 matches current source bytes; this audit
does not establish that fact.

## Minimum case matrix

| Area | Positive | Negative |
|---|---|---|
| Inventory | Canonical target plus canonical valid nonmatching ID | Non-JSON, malformed/object-without-ID, unsafe ID, wrong schema, ID/path mismatch, reparse/non-regular entry |
| Snapshot | Nonempty structured records and matching digest | Empty/malformed baseline, digest mismatch, dangling certificate link |
| Protocol | Exactly ready/calling/result with all op/ID bindings and six valid attestation entries | Missing/wrong op or ID, extra/nonmapping event, missing/extra attestation entry, invalid SHA, wrong result shape |

The smallest future patch is confined to the four named verifier functions and
small local validators. It must not change production custody, C#, CLI, C1, or
C2a behavior. Cross-process atomic marker deletion is outside this audit's
conclusions.
