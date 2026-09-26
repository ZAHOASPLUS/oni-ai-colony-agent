# Functional-building flooded API audit

The local game assembly was inspected before adding the flooded telemetry
sibling. The input was:

- `C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed\Assembly-CSharp.dll`
- SHA-256: `A58E04D0FFDF89B86FB28B71AD900625B3B539DB30D67F8C6269F73A9F5AE599`
- Last-write time (UTC): `2026-07-29 21:02:37`

`ilspycmd -t Floodable` inspection confirmed these public members in that
exact assembly:

| Type | Member | Use |
| --- | --- | --- |
| `Floodable` | `bool IsFlooded` | nullable `is_flooded` field |

The reader enumerates only completed `BuildingComplete` instances in the
active world and binds each record to its instance and world identity. A
missing `Floodable` component or getter exception remains `null` (unknown).
The identity, world binding, registry, and second-read races fail the
envelope closed. It does not inspect liquid mass, element type, placement
cells, or derive a safety/operational conclusion, and it exposes no actions.
