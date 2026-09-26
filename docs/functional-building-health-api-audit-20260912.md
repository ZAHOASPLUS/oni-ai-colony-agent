# Functional-building health API audit

The local game assembly was inspected before adding the health telemetry
sibling. The input was:

- `C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed\Assembly-CSharp.dll`
- SHA-256: `A58E04D0FFDF89B86FB28B71AD900625B3B539DB30D67F8C6269F73A9F5AE599`
- Last-write time (UTC): `2026-07-29 21:02:37`

`ilspycmd` inspection confirmed these public members in that exact assembly:

| Type | Member | Use |
| --- | --- | --- |
| `BuildingEnabledButton` | `bool IsEnabled` | nullable `enabled` field |
| `BuildingHP` | `int HitPoints` | nullable `hit_points` field |
| `BuildingHP` | `int MaxHitPoints` | nullable `max_hit_points` field |
| `BuildingHP` | `bool IsBroken` | nullable `is_broken` field |
| `BuildingHP` | `bool NeedsRepairs` | nullable `needs_repairs` field |

`Health.hitPoints`, `Health.maxHitPoints`, and `Health.percent()` are also
present, but the sibling uses the building-specific `BuildingHP` surface and
does not depend on the generic `Health` component. The reader catches missing
components and getter failures as `null`; identity, world-binding, registry,
and read races fail the envelope closed. The existing operational field is not
copied into this payload.
