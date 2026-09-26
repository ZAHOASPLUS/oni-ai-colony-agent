# A09A-2 command-custody core

This document records the independent offline gate for the shared command
custody module. It is deliberately narrower than the command queue, approval,
Bridge, survival, and live-game gates. A pass here does not authorize a game
launch or consumer integration.

## Scope and safety boundary

The verifier is `scripts/verify_command_custody_core.py`. It uses only fresh
named directories under the system Temp location and runner-created child
processes. It does not start ONI, Klei, Steam, Bridge, a verifier helper, or a
real approval path; it does not read a cloud save, write a Klei save, modify a
Mod/configuration, or change VPN/Clash/Mihomo/TUN/network state.

The allowed source is `agent/command_custody.py`. `agent/command_queue.py` is a
consumer boundary and must remain unchanged in this task. Evidence is written
only below `reports/a09a2-command-custody-core/`.

## Required command and evidence

Run exactly:

```text
python -B scripts/verify_command_custody_core.py
```

The command must exit `0`. The summary is intentionally short; the full JSON
report and JSONL event log retain case results, assertion counts, child PIDs and
exit codes, timing, inventory snapshots, source hashes, forbidden-process
census, and Temp cleanup evidence.

The verifier proves or rejects, using real filesystem state and deterministic
`multiprocessing` Events/Pipes/Barriers:

- process exclusion, finite timeout, crash release, and publication/disposition
  interleavings;
- duplicate publication and same-id/different-body rejection;
- injected preclaim, claimed, staged, published, fsync, revalidation, and
  cleanup boundaries;
- exact journal schema/keyset, malformed/unreadable/duplicate journal and
  orphan-payload fail-closed behavior;
- protected-stage status/path tampering, hardlinks, reparse/junction
  ancestors, unsafe ancestors, and moved-directory rejection; and
- unrelated replacement survival during cleanup.

For each transition the report counts command owners in every protected stage,
records bytes/hashes/identities where readable, and distinguishes a verified
pending owner, a verified terminal owner, or an explicitly blocked unknown
state. An exception swallowed as success is a verifier failure.

The production boundary keeps the lifecycle lock pathname persistent. On
Windows, an open/share violation while another process owns the lock is treated
as bounded lock contention and becomes `CommandCustodyTimeout`; it is never
repaired by deleting the lock file. Bridge/commands directory identities are
bound as they are observed, so a moved or recreated root fails closed instead
of being silently rebuilt. Disposition recovery accepts a terminal destination
only when its recorded transaction identity and exact bytes/hash prove the
outcome. If the terminal rename has consumed the replayable final-payload
evidence, the source-absent path may complete only in the journal's
`completing` phase, after the terminal inode, canonical status, exact
bytes/hash, and journal bindings have been revalidated. Cleanup performs a
final identity/byte revalidation so a replacement file is retained and
reported as `CommandCustodyUnknown`. The raw create/rename helpers
remain module-private; callers use the custody APIs above.

## Review handoff

The report must state the exact run timestamps, source before/after hashes,
range-scope hash for `command_queue.py`, case/assertion counts, child census,
Temp cleanup result, and remaining limitations. The only remaining authority
after an offline core pass is a separately bounded consumer/approval/Bridge
integration review followed by the broader ONI safety gates. This document is
not live-game evidence.
