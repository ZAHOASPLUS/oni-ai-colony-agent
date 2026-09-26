# C2a 801DE9 retry static review

Action A09A9-C2A-RETRY-STATIC-SEAL-01, 2026-09-07T12:31Z.
Commander: 01a07bd2-da7d-7170-bfae-85bc87f7c69c, actual Astra Ultra.
Independent read-only audit: /root/c2a_retry_readonly.
Source was read and hashed before/after; no target execution or source edit.

Source `scripts/verify_confirmation_custody_integration.py`:
801DE95402039CACFCB40EB0E1761ACC7E2ECECE0488469D636D1DBE204724A4.
Supplement `docs/a09a9c2a-four-cleanup-fake-supplement-20260907.md`:
CDA0E9E10F9E87B582241B830FB6876220DFAB7F8D4274729121F9003F1E9C12.
These are static findings pending independent fake evidence. The full goal
remains active; no fake, Windows-resource, production or live gate is passed.

## Marker deletion is not persistent across cleanup attempts

At source lines 2001-2006, successful unlink and final absence are recorded
only in the local marker_cleanup result. The ledger retains creation_owned,
binding_observed and its previously closed read handle. The next cleanup
initializes another result and reaches _capture_marker_binding at line 1967.
At lines 692 and 720-743 that function clears binding_observed, archives the
closed read handle, resets current handle_closed=False and opens the already
deleted path. This yields a new FileNotFoundError and regresses current
marker evidence to removed=False, present_after=None, handle_closed=False.

Normal _spawn_worker acquisition followed by a first stdin/stdout/stderr
close failure can reach this state: independent marker cleanup still succeeds,
and the next close may recover while a new marker error is introduced. This
conflicts with supplement lines 63-71. A first marker read-close failure may
instead retain the marker and recover on its second cleanup; that distinct
case must not be used to claim all cleanup retries work. Keeping the first
attempt's errors as a cumulative prefix is correct and is not this defect.

Candidate minimum repair, not an authorization or implementation: persist
unlink effects and final-presence observation in the ledger. Preserve closed
handle evidence on retries. An already removed marker should not be reopened
as a new owned read handle. A recreated/replaced path must be preserved and
reported, not rebound or deleted. Unknown unlink effects/presence need explicit
uncertainty and bounded observation. Keep historical errors and kill history.

## Unknown process ownership is treated as authority

At source line 1652, process is nonempty and process_owned is not False
authorizes process handling. A missing field receives False from defaults;
explicit None, 0, strings and other invalid values remain and satisfy the
identity comparison. This can permit poll, timeout kill/wait and process
stdin acquisition/close (1666-1734). Existing ledger stdin also has no strict
ownership gate. Missing-reader output closure separately requires explicit
True (1791-1804), so this finding does not imply every output pipe closes.

Reachability limit: the normal caller creates a fresh ledger at 2241, and
successful Popen immediately registers process_owned=True at 1317-1318.
There is no observed normal source path writing None/invalid ownership.
This is a static direct-ledger boundary defect, covered by the supplement's
explicit unknown/missing-ownership and no-unowned-operation requirements.
It is not evidence of a real process being killed.

Candidate minimum repair: only explicit, sourced True authorizes process
operations and process-derived stdin handling. Nonempty objects with missing,
unknown or invalid ownership must retain residual uncertainty, not become
owned or be reported absent. Preserve the existing lifecycle provenance rule.

## Current exclusive author and next boundary

The only helper writer is /root/c2a_fresh_fake, actual Luna Max, thread
01a07bd4-b319-7140-ab9d-2a3b826de4d2. It may write only under:
reports/a09a9c2a-fresh-fake-20260907T122715Z-pid-45864-nonce-34bcda8fcacd4bb2/.
It is building a fresh full fake helper under the immutable contract and
supplement. Positive-first, exact faults, three independent outcomes and
actual source/doc/helper/six-source binding remain mandatory. It cannot edit
source, mutate ledger to conceal these defects, or suppress unrelated errors.
The old helper and attempts01-05 remain immutable and unexecuted here.

After a reported source defect, freeze that author's helper/report and verify
terminal status and actual outer exit. Any repair needs a separate explicit
action checkpoint, actual preimage copies and source hash guards. This review
does not open source writing or real-resource execution. All failed evidence
must remain. Original saves/previews, normal Mod/config, real queues and
universal helper absence remain unverified; no game action was sent.
