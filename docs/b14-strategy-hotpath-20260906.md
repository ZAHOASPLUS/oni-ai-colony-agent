# B14 strategy hot-path experiment — 2026-09-06

Action ID: `A09A2_LUNA_B14_HOTPATH_20260906_01`

## Outcome

The bounded offline experiment is **blocked / not a performance pass**. The
focused semantic differential matched exactly after the tentative local
optimization, but the one permitted 10,000-line startup-bound subprocess
benchmark did not establish a current session. It therefore cannot prove the
8.0-second gate or authorize any live-game action. The tentative source patch
was rolled back to the fresh baseline.

No ONI/Klei/Steam/Bridge process was launched. No original cloud save, normal
Mod configuration, network/VPN/Clash/Mihomo/TUN state, command queue, or command
custody state was read or changed.

## Scope and files

The allowed harness is [verify_b14_strategy_hotpath.py](../scripts/verify_b14_strategy_hotpath.py).
Evidence is under `reports/b14-strategy-hotpath-20260906/`:

- `baseline/differential.json` and `baseline/outputs.json` — five deterministic cases captured before the tentative patch;
- `post-patch.json` — the single post-patch differential and benchmark result;
- `recovery-packet.md` — rollback, safety invariants, hashes, and the next single action.

The attempted source change was limited to the `safe_get`/`field_quality`
top-level lookup path and duplicate `field_quality` reuse in
`_field_contract_blockers`. It introduced no cross-session cache and changed no
validation, freshness, world-scope, startup-binding, queue, or threshold logic.

## Semantic evidence

Five cases were exercised: normal, missing/invalid fields, stale state,
multi-world plus contract blockers, and an emergency suffocation incident.
The full JSON strategy outputs matched exactly before and after the tentative
patch. Baseline output SHA-256 values were:

```text
normal                2bf30fc95d84d203a5d390612c6b9bfbe387b795c641af4fae390cfbb58d5994
missing_invalid       7950d668cc45219dfc8fa29ef65fed901a93deb7e7907469066804a3821f8905
stale                 b6f8c9a92958d5b7b3924a8cbf29daf35c4155b03aeef8838077cfd058ab36a2
multi_world_contract  53b8fb385ef63ba2a250cdedca37b54c39f59721d0d2ccc181bf7fd97d16c868
emergency_incident    ef49396265d8ae4926fbc6e9be70b951f81743c4c6606219d38deea243d69711
```

The focused differential result is `match=true` in `post-patch.json`.

## Single benchmark result

Fixture: 10,000 records, 52,182,376 bytes, one synthetic startup envelope and
9,999 state snapshots, isolated in a `TemporaryDirectory`.

Observed values:

```text
return code             0
stderr                  empty
subprocess wall time    0.7783388999960152 s
peak working set        133.6328125 MiB
hard timeout            20.0 s (not reached)
archive lines           10,000
temporary files         none
child residual          none
temporary directory     cleaned
latest_seq              null                FAIL
health                  waiting_current_session_snapshot  FAIL
startup binding         false               FAIL
```

The subprocess archived every input line but the startup-bound runtime did not
accept the synthetic session as the current target. This is a fixture-admission
failure, not evidence that the optimization passes. The harness was not rerun
and no threshold was changed.

## Hashes and rollback

Final source hashes after rollback:

```text
agent/strategy.py                       926F3597A4057E210E619EF203FD2B33ECE82DF3A03DEC3B76734E51C368E87E
agent/state_quality.py                  99C1B0D6F3AF6AEE4D4CC0F309973932AF7F4E99BE5C6C309DBD3D7F5653C14A
scripts/verify_b14_strategy_hotpath.py  BF95EFDD3390FE65D894EA7221D848EE3A2C5EE12095EBA7A4D21CBBBDE3024F
bridge/oni_bridge.py                    0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C
scripts/verify_bridge_reliability.py    FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337
reports/bridge-reliability.json         11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D
```

`python -B -m py_compile agent/strategy.py agent/state_quality.py scripts/verify_b14_strategy_hotpath.py`
completed with exit code 0 before the single benchmark. The exact tentative
post-patch source hashes and rollback details are preserved in
`recovery-packet.md`.

Next action requires a fresh checkpoint: inspect only the synthetic startup
envelope acceptance path, repair the dedicated fixture if justified, capture a
new baseline, and then decide whether another bounded experiment is authorized.
Do not treat this artifact as B13 gate success or live authorization.
