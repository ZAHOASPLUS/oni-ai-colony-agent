# Reachability API audit

This is a read-only IL2CPP surface audit. It does not start ONI, load a save,
refresh reachability, enqueue an action, or grant live admission.

The inspected local assembly is
`C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed\Assembly-CSharp.dll`,
SHA-256 `A58E04D0FFDF89B86FB28B71AD900625B3B539DB30D67F8C6269F73A9F5AE599`.

Confirmed native members from the checked-in IL2CPP audit extracts:

| Type | Member | Safety interpretation |
| --- | --- | --- |
| `Workable` | `GetCell()` | Read the workable's native anchor cell |
| `Workable` | `GetOffsets()` / `GetOffsets(int)` | Read native approach offsets |
| `MinionGroupProber` | `Get()` | Obtain the current reachability probe, nullable |
| `MinionGroupProber` | `IsReachable(int)` / `IsReachable(int, CellOffset[])` | Read current reachable-cell state |
| `MinionGroupProber` | `IsAllReachable(int, CellOffset[])` | Native building/workable reachability query |
| `ReachabilityMonitor` | `isReachable` | Cached state machine parameter; not used as the producer |
| `Workable` | `RefreshReachability()` | Explicitly excluded because it actively refreshes state |

The safe next implementation is an optional `Workable` sibling on completed
active-world buildings. It should return nullable `is_reachable`, preserve
unknown when `Workable` or `MinionGroupProber` is missing, bind every record to
the active world and instance identity, and perform a second read to reject
mixed-time results. It must not call `RefreshReachability()` or infer food
access, operational state, hazard safety, or network connectivity.

The native audit alone is not runtime evidence. A future clone-only test must
verify the producer against a named save clone with preserved source hashes,
then reconcile the observed value with the game log and process boundary.
