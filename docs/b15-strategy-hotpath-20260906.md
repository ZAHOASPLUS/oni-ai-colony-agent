# B15 strategy hot-path report — 2026-09-06

Status: **failed closed; rolled back**

Action: `A09A2_LUNA_B15_FIXTURE_HOTPATH_20260906_01`

This was a bounded offline task. No ONI/Klei/Steam/Bridge process was launched, no original save or normal Mod/config was touched, and no network/VPN/Clash/Mihomo/TUN or command-custody state was changed. The protected Bridge files remained at their confirmed baseline hashes.

The required baseline ran once before patching and returned zero for the five differential cases: `normal`, `missing_invalid`, `stale`, `multi_world_contract`, and `emergency_incident`. The source patch compiled successfully. The required post-patch command ran exactly once.

The five-case differential matched the baseline. Before the 10,000-record measurement, the admission sanity confirmed `session-performance`, `latest_seq=2`, `health=ok`, and a verified startup binding. Its cleanup assertion reported `temporary_directory_cleaned=false`, so the harness correctly skipped the 10k benchmark. Consequently there is no valid performance measurement and no B15 completion claim.

The failure artifact is [reports/b15-strategy-hotpath-20260906/post-patch.json](../reports/b15-strategy-hotpath-20260906/post-patch.json). The complete rollback evidence is [reports/b15-strategy-hotpath-20260906/recovery-packet.md](../reports/b15-strategy-hotpath-20260906/recovery-packet.md).

The production hot-path files `agent/strategy.py` and `agent/state_quality.py` were immediately restored with the inverse patch. The harness was not fully reverted: it retains only the dedicated fixture `reason=save_loader_load_complete` correction, while its report root is back at the historical B14 location. Its current SHA-256 is `69CDE918F295BB754CF27624B42C1F2194FA8E98611389E5BA6CF39C0831BD4E`, which is not the original B14 harness hash. No rerun or threshold change was made. Offline evidence does not authorize a live-game action.
