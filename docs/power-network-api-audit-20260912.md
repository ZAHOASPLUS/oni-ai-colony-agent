# Power network API audit

This is a read-only local IL2CPP audit. It does not start ONI, load a save,
call `Connect`/`Disconnect`, rebuild networks, enqueue commands, or grant live
admission.

The inspected `Assembly-CSharp.dll` is the installed local build with SHA-256
`A58E04D0FFDF89B86FB28B71AD900625B3B539DB30D67F8C6269F73A9F5AE599`.

Confirmed members:

| Type | Member | Safe use |
| --- | --- | --- |
| `Wire` | `IsConnected` | Read whether the wire cell belongs to an electrical network |
| `Wire` | `NetworkID` | Read the current electrical network identifier; unknown on absent network |
| `Wire` | `GetWireConnections()` | Read physical wire connection flags when a Wire component is present |
| `IUtilityNetworkMgr` | `GetNetworkForCell(int)` | Read network membership for a declared utility cell |
| `IUtilityNetworkMgr` | `GetConnections(int, bool)` | Read utility connection flags |
| `Conduit` | `GetNetworkManager()` | Obtain the gas/liquid utility manager for an existing conduit |

The safe next slice is a nullable power membership sibling for completed
active-world buildings. It may report wire connection and network identity,
but must not infer power production, load coverage, circuit sufficiency, or
functional status. `Wire.Connect`, `Wire.Disconnect`, and
`ForceRebuildNetworks()` are excluded because they mutate or refresh network
state. Building conduit endpoint records remain a separate follow-up because
their native endpoint fields are private and require a carefully bounded cell
manager query.

The API audit is not runtime evidence. A clone-only test must still verify the
producer against a named save clone and reconcile the result with process,
queue, and log boundaries.
