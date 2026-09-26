# Accepted CLI Audit: Shortage Blueprint Readiness

- Run: `oni-v27-final-cli-audit-20260711`
- Task: `blueprint-readiness-audit`
- Worker: `oni-v27-cheap-3` (cheap pool)
- Review state: accepted
- Worker diff: empty (`E3B0C442...B855`)
- Source result: `C:\Users\user\AppData\Local\CodexFleet\state-oni-v27\runs\oni-v27-final-cli-audit-20260711\tasks\blueprint-readiness-audit\final.md`

## Main-Thread Disposition

The worker's key finding was valid and is fixed. Blueprint runtime acceptance now requires observed `materials_available=false`, a non-empty selected-material mass vector containing only numeric zeroes, and observed quality for both inventory fields.

The final evidence proves:

- Ladder selected `WoodLog` with `available_mass_kg=[0.0]` at cell `138741`.
- Wire selected `Cinnabar` with `available_mass_kg=[0.0]` at cells `139386,139387`.
- Both placements created unfinished waiting-delivery blueprints through `TryPlace`.
- Exact-instance cancellation restored every original fingerprint.
- A stocked fixture is explicitly rejected as shortage-runtime evidence.

No direct build, sandbox, instant-build, injection, or save-edit path was introduced.

## Review Notes

The worker recovered from read-only policy rejections. Effective input was 94,616 tokens against a 30,000 task limit. The scheduler budget variance is recorded and accepted; no expensive provider was used.
