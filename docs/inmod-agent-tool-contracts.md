# In-mod v1.2 P0 tool inventory

Generated from the shared schema and C# registry. This is a source inventory, not a live game test.

Schema tools: 34. C# handlers: 33. Async adapter: `wait`.

| Tool | Main-thread handler | Requires game | Write ownership | Required arguments | Optional arguments |
|---|---|---|---|---|---|
| get_lifecycle | _ => ApiLifecycle.Describe() | no | read | none | none |
| load_save | ApiLifecycle.LoadSaveTool | no | write | path | none |
| get_status | ApiStatusReader.Read | yes | read | none | world |
| get_map | ApiMapReader.Read | yes | read | none | x, y, w, h, layers, world |
| inspect | ApiInspectReader.Read | yes | read | x, y | none |
| get_buildable | ApiCatalogReader.ReadBuildable | yes | read | none | category, include_locked, world |
| get_research | ApiCatalogReader.ReadResearch | yes | read | none | scope |
| get_events | ApiEvents.Read | yes | read | none | since, limit |
| build | ApiActions.Build | yes | write | def_id, x, y | rotation, materials, priority |
| build_path | ApiActions.BuildPath | yes | write | def_id, cells | materials, priority |
| dig | ApiActions.Dig | yes | write | x, y | w, h, priority |
| deconstruct | ApiActions.Deconstruct | yes | write | none | id, x, y, w, h, priority |
| cancel | ApiActions.CancelArea | yes | write | x, y | w, h |
| set_priority | ApiActions.SetPriorityTool | yes | write | priority | id, x, y, w, h |
| set_research | ApiActions.SetResearch | yes | write | tech | require_ready |
| set_speed | ApiActions.SetSpeed | yes | write | speed | time_scale |
| save_checkpoint | ApiActions.SaveCheckpoint | yes | write | none | note |
| list_buildings | ApiQueries.ListBuildings | yes | read | none | def, problems_only, include_structural, world, x_min, x_max, y_min, y_max, offset |
| get_resources | ApiQueries.ReadResources | yes | read | none | world |
| find | ApiQueries.Find | yes | read | kind | query, limit, x, y, world |
| get_progress | ApiQueries.ReadProgress | yes | read | none | none |
| harvest | ApiPlayTools.Harvest | yes | write | x, y | w, h, enable, priority |
| sweep | ApiPlayTools.Sweep | yes | write | x, y | w, h, priority |
| mop | ApiPlayTools.Mop | yes | write | x, y | w, h, priority |
| configure | ApiPlayTools.Configure | yes | write | id | recipe, count, seed, storage_filter, element_filter, assigned_duplicant, massage_start_stress, massage_stop_stress, crew, rocket_restriction, rocket_action, smart_battery_low_pct, smart_battery_high_pct, study, capture, critter_limit, request_suit, warp_action, enabled |
| set_chore_priority | ApiPlayTools.SetChorePriority | yes | write | duplicant, chore_group, level | none |
| set_schedule | ApiPlayTools.SetSchedule | yes | write | blocks | schedule |
| get_skills | ApiSkills.Read | yes | read | none | none |
| learn_skill | ApiSkills.Learn | yes | write | duplicant, skill | none |
| move_to | ApiMovement.MoveTo | yes | write | duplicant, x, y | none |
| get_printables | ApiPrintables.Read | yes | write | none | none |
| choose_printable | ApiPrintables.Choose | yes | write | index | none |
| reject_printables | ApiPrintables.Reject | yes | write | none | none |
| wait | GameToolDispatcher.WaitAsync | yes | read | none | seconds, since |

## Known contract limits

- Formal schemas have no machine-readable defaults or return schemas. Handler defaults remain authoritative; no defaults were invented.
- Shared validation now rejects required/type/enum/range/array errors before handlers, including explicit null where the schema forbids it. This intentionally tightens previously permissive direct HTTP calls.
- Domain-dependent checks still run inside handlers. `configure` can partially apply earlier settings before a later domain-specific error; this is not transactional.
- `get_printables` generates/caches offers and consumes RNG, so it is now write-lease-sensitive. Offer and event caches reset on load/stop boundaries.
- `load_save` participates in shared write ownership, but the in-mod dispatcher refuses it because paths are not a native autosave capability.
- Async `wait` preserves initial backlog handling, 2-second polling and emergency events; adds explicit missed/truncated signals with a 512-event cap.
- Full game results are bounded in model context with explicit truncation; no claim of complete large-state delivery.
- New-game tool names and handlers are not added until actual ONI controller APIs are verified.
