# Accepted CLI Audit: Runtime Evidence

- Run: `oni-v27-final-cli-audit-20260711`
- Task: `runtime-evidence-audit`
- Worker: `oni-v27-cheap-2` (cheap pool)
- Review state: accepted
- Worker diff: empty (`E3B0C442...B855`)
- Source result: `C:\Users\user\AppData\Local\CodexFleet\state-oni-v27\runs\oni-v27-final-cli-audit-20260711\tasks\runtime-evidence-audit\final.md`

## Main-Thread Disposition

The worker's valid evidence findings were implemented after review:

- Final lineage verification now requires the exact 14-step order.
- Every checkpoint must contain at least one immutable artifact under the lineage artifact root.
- Runtime action checks bind to the session immediately before their own checkpoint; duplicate replay separately binds original and replay sessions.
- Disabled transition policies must have zero allowed save hashes.
- Every explicit checkpoint save must rotate the save hash.
- Runtime markers bind the lineage run id, latest checkpoint hash, current save hash, and runtime-report hash.
- Completion audit rejects stale or incomplete `game_verify` result sets.

The strengthened verifier replayed the existing final lineage successfully, so no evidence was synthesized and no game action was rerun.

## Review Notes

The worker recovered from read-only policy rejections and Windows regex/glob quoting errors. Effective input was 95,413 tokens against a 35,000 task limit. The scheduler budget variance is recorded and accepted; no expensive provider was used.
