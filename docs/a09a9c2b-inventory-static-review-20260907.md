# C2b inventory static review

Action A09A9-C2B-STATIC-SEAL-01, 2026-09-07.
Commander: 01a07bed-714f-7550-946c-b8888193a737, actual Astra Ultra.
Independent read-only reviewer: /root/c2b_readonly_inventory, terminal.
Source before/after measured by reviewer at 12:56:43Z and again by commander:
801DE95402039CACFCB40EB0E1761ACC7E2ECECE0488469D636D1DBE204724A4
scripts/verify_confirmation_custody_integration.py.
No target execution, source repair or live admission. Full goal remains active.
Current sole C2a helper author 01a07be8 remains separate and active; this note
neither changes its scope nor permits another author.

## Confirmed static boundaries

1. _command_locations line372 uses case-sensitive endswith('.json') before
lstat/reparse/JSON checks. A valid canonical done plus failed/extra.JSON with
the target id is counted as one owner because extra.JSON is skipped. Bad JSON,
unreadable candidates and links with uppercase suffix are also skipped.
Minimum repair: casefold the suffix before entering existing checks.

2. _read_json line140 only requires dict; _command_locations line382 compares
id before validating command identity. Additional failed/<id>.json containing
{}, missing/null/empty/wrong-type id silently drops out of ownership inventory.
An extra canonical confirming path is separately blocked by lexists; do not
claim that case silently passes. Validate a nonempty string id (and required
command structure) before excluding a valid non-target record.

3. _protected_snapshot line327 calls exists on three certificate paths before
_stable_file_record. A dangling link can therefore be recorded as absent. A
baseline-absent path changed to a dangling link can preserve the snapshot digest
when the commands tree is unchanged. Normal-file to dangling-link already
changes file to absent; that is not the silent counterexample. Use explicit
lstat, catch only FileNotFoundError as absence, propagate other errors and
reject reparse/nonregular objects before stable reads.

Current outer propagation can carry these failures: _inspect_final_state
line452 retains errors, _assert_final_state line465 requires zero errors,
and _sample_while_locked line1523 retains sample errors. No outer protocol
change is needed for these three repairs.

## Pending acceptance oracles

- Canonical valid done plus another-stage extra.JSON or extra.JsOn of the same
  id must count the second owner and fail uniqueness.
- Uppercase JSON with invalid JSON, unreadability or dangling link must fail
  inventory explicitly, never disappear from a successful result.
- Additional lowercase JSON containing {}, null id, empty id or integer id
  must fail object validation rather than be treated as valid non-target data.
- An additional structurally valid different-id command remains permitted and
  does not change the target count.
- For each certificate label, baseline absent to dangling link must cause a
  snapshot error, not the identical successful absence digest.
- True absence still records absent; non-FileNotFoundError lstat errors propagate;
  plain regular files keep their stable-file record behavior.

These are unexecuted oracles. A separate source action requires actual preimage
copies and fresh hash guards after the current C2a atomic step. C2a full fake,
inert Windows resources, C2b execution, production confirmation, C# integration,
bootstrap, verified clone/G0 and sustainable survival gates remain pending.
