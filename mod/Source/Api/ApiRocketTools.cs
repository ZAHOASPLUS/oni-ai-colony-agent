using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using STRINGS;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Native Spaced Out! rocketry hooks: module attachment on LaunchPads,
    /// crew assignment, interior access restriction, orbit destination,
    /// launch/cancel, landing, and inspect telemetry.  Uses the game's own
    /// SelectModuleCondition and ProcessCondition checks without decision logic.
    /// </summary>
    internal static class ApiRocketTools
    {
        private static readonly ProcessCondition.ProcessConditionType[] RocketConditionTypes =
        {
            ProcessCondition.ProcessConditionType.RocketPrep,
            ProcessCondition.ProcessConditionType.RocketStorage,
            ProcessCondition.ProcessConditionType.RocketBoard,
            ProcessCondition.ProcessConditionType.RocketFlight
        };

        internal static bool IsRocketModuleDef(BuildingDef def)
        {
            return def?.BuildingComplete != null && def.BuildingComplete.GetComponent<RocketModuleCluster>() != null;
        }

        internal static GameObject PlaceRocketModule(BuildingDef def, int cell, List<Tag> materials)
        {
            int targetX = Grid.CellColumn(cell);
            int targetY = Grid.CellRow(cell);
            byte worldId = Grid.WorldIdx[cell];

            RocketModuleCluster directModule = FindModuleAtCell(cell);
            LaunchPad pad = FindLaunchPadForCell(cell, targetX, targetY, worldId);
            if (directModule == null && pad != null && pad.HasRocket())
            {
                CraftModuleInterface craft = pad.LandedRocket.CraftInterface;
                if (craft != null && craft.ClusterModules.Count > 0)
                {
                    directModule = craft.ClusterModules[craft.ClusterModules.Count - 1].Get();
                }
            }

            ReorderableBuilding moduleRules = def.BuildingComplete.GetComponent<ReorderableBuilding>();
            if (moduleRules == null)
            {
                throw new ApiException("invalid_placement", def.PrefabID + " has no ReorderableBuilding component.");
            }

            if (directModule == null)
            {
                if (pad == null)
                {
                    throw new ApiException(
                        "invalid_placement",
                        def.PrefabID + " must be placed on a finished LaunchPad or existing rocket module.",
                        "Pass the (x, y) of the LaunchPad or an existing module on it.");
                }
                VerifyModuleConditions(moduleRules, pad.gameObject, def, SelectModuleCondition.SelectionContext.AddModuleAbove);
                GameObject basePlaced = pad.AddBaseModule(def, materials);
                if (basePlaced == null)
                {
                    throw new ApiException("invalid_placement", "The game refused to place base module " + def.PrefabID + ".");
                }
                return basePlaced;
            }

            ReorderableBuilding existingPrefabRules = Assets.GetPrefab(directModule.GetComponent<KPrefabID>().PrefabID())
                ?.GetComponent<ReorderableBuilding>();
            bool addBelow = (existingPrefabRules != null && existingPrefabRules.buildConditions.Any(cond => cond is TopOnly)) ||
                moduleRules.buildConditions.Any(cond => cond is EngineOnBottom);
            SelectModuleCondition.SelectionContext context = addBelow
                ? SelectModuleCondition.SelectionContext.AddModuleBelow
                : SelectModuleCondition.SelectionContext.AddModuleAbove;

            VerifyModuleConditions(moduleRules, directModule.gameObject, def, context);
            GameObject added = directModule.GetComponent<ReorderableBuilding>().AddModule(def, materials);
            if (added == null)
            {
                throw new ApiException("invalid_placement", "The game refused to attach module " + def.PrefabID + ".");
            }
            return added;
        }

        private static void VerifyModuleConditions(
            ReorderableBuilding moduleRules,
            GameObject target,
            BuildingDef def,
            SelectModuleCondition.SelectionContext context)
        {
            List<string> failures = new List<string>();
            foreach (SelectModuleCondition condition in moduleRules.buildConditions)
            {
                if (condition == null)
                {
                    continue;
                }
                if (!condition.EvaluateCondition(target, def, context))
                {
                    string reason = UI.StripLinkFormatting(condition.GetStatusTooltip(false, target, def) ?? condition.GetType().Name);
                    if (!string.IsNullOrWhiteSpace(reason))
                    {
                        failures.Add(reason.Trim());
                    }
                }
            }
            if (failures.Count > 0)
            {
                throw new ApiException(
                    "invalid_placement",
                    def.PrefabID + " cannot attach here: " + string.Join("; ", failures));
            }
        }

        private static RocketModuleCluster FindModuleAtCell(int cell)
        {
            for (int layer = 0; layer < (int)ObjectLayer.NumLayers; layer++)
            {
                GameObject go = Grid.Objects[cell, layer];
                RocketModuleCluster module = go == null ? null : go.GetComponent<RocketModuleCluster>();
                if (module != null)
                {
                    return module;
                }
            }
            return null;
        }

        private static LaunchPad FindLaunchPadForCell(int cell, int x, int y, byte worldId)
        {
            for (int layer = 0; layer < (int)ObjectLayer.NumLayers; layer++)
            {
                GameObject go = Grid.Objects[cell, layer];
                LaunchPad atCell = go == null ? null : go.GetComponent<LaunchPad>();
                if (atCell != null && go.GetComponent<BuildingComplete>() != null)
                {
                    return atCell;
                }
            }
            foreach (LaunchPad candidate in Components.LaunchPads.Items)
            {
                if (candidate == null || candidate.GetComponent<BuildingComplete>() == null)
                {
                    continue;
                }
                int padCell = Grid.PosToCell(candidate.gameObject);
                if (Grid.WorldIdx[padCell] == worldId &&
                    Grid.CellColumn(padCell) == x &&
                    y >= Grid.CellRow(padCell) &&
                    y <= Grid.CellRow(padCell) + 35)
                {
                    return candidate;
                }
            }
            return null;
        }

        internal static Clustercraft ResolveCraft(GameObject building, bool required)
        {
            if (building == null)
            {
                if (required)
                {
                    throw new ApiException("unsupported_setting", "Target building was not found.");
                }
                return null;
            }
            RocketModuleCluster module = building.GetComponent<RocketModuleCluster>();
            if (module?.CraftInterface != null)
            {
                return module.CraftInterface.GetComponent<Clustercraft>();
            }
            LaunchPad pad = building.GetComponent<LaunchPad>();
            if (pad?.LandedRocket?.CraftInterface != null)
            {
                return pad.LandedRocket.CraftInterface.GetComponent<Clustercraft>();
            }
            RocketControlStation station = building.GetComponent<RocketControlStation>();
            if (station != null)
            {
                WorldContainer myWorld = station.GetMyWorld();
                Clustercraft interiorCraft = myWorld == null ? null : myWorld.GetComponent<Clustercraft>();
                if (interiorCraft != null)
                {
                    return interiorCraft;
                }
            }
            if (required)
            {
                throw new ApiException(
                    "unsupported_setting",
                    "This building is not a rocket module, LaunchPad with a landed rocket, or RocketControlStation.");
            }
            return null;
        }

        internal static object SetCrew(GameObject building, JArray names)
        {
            Clustercraft craft = ResolveCraft(building, required: true);
            PassengerRocketModule passenger = craft.ModuleInterface.GetPassengerModule();
            if (passenger == null)
            {
                throw new ApiException("unsupported_setting", "This rocket has no passenger habitat module.");
            }
            AssignmentGroupController groupController = passenger.GetComponent<AssignmentGroupController>();
            if (groupController == null || !Game.Instance.assignmentManager.assignment_groups.ContainsKey(groupController.AssignmentGroupID))
            {
                throw new ApiException("internal_error", "Passenger module assignment group is not initialized yet.");
            }

            List<MinionIdentity> chosen = new List<MinionIdentity>();
            foreach (JToken token in names)
            {
                chosen.Add(ApiDuplicants.Find(token, "each crew entry"));
            }

            AssignmentGroup group = Game.Instance.assignmentManager.assignment_groups[groupController.AssignmentGroupID];
            foreach (IAssignableIdentity existing in group.GetMembers().ToList())
            {
                group.RemoveMember(existing);
            }
            foreach (MinionIdentity identity in chosen)
            {
                MinionAssignablesProxy proxy = identity.assignableProxy.Get();
                if (proxy != null && !group.HasMember(proxy))
                {
                    group.AddMember(proxy);
                }
            }
            passenger.RequestCrewBoard(
                chosen.Count > 0
                    ? PassengerRocketModule.RequestCrewState.Request
                    : PassengerRocketModule.RequestCrewState.Release);
            return chosen.Select(item => (object)new Dictionary<string, object>
            {
                { "id", ApiDuplicants.Id(item) },
                { "name", item.GetProperName() }
            }).ToList();
        }

        internal static object SetRestriction(GameObject building, string mode)
        {
            Clustercraft craft = ResolveCraft(building, required: true);
            WorldContainer interior = craft.ModuleInterface.GetInteriorWorld();
            if (interior == null)
            {
                throw new ApiException("unsupported_setting", "This rocket has no interior world.");
            }
            List<RocketControlStation> stations = Components.RocketControlStations.GetWorldItems(interior.id);
            if (stations == null || stations.Count == 0)
            {
                throw new ApiException("unsupported_setting", "No RocketControlStation exists inside the rocket interior.");
            }
            bool restrict;
            if (string.Equals(mode, "crew_only", StringComparison.OrdinalIgnoreCase))
            {
                restrict = true;
            }
            else if (string.Equals(mode, "unrestricted", StringComparison.OrdinalIgnoreCase))
            {
                restrict = false;
            }
            else
            {
                throw new ApiException("invalid_argument", "rocket_restriction must be crew_only or unrestricted.");
            }
            foreach (RocketControlStation station in stations)
            {
                station.RestrictWhenGrounded = restrict;
            }
            return restrict ? "crew_only" : "unrestricted";
        }

        internal static object ExecuteRocketAction(GameObject building, string action)
        {
            if (string.Equals(action, "orbit", StringComparison.OrdinalIgnoreCase))
            {
                Clustercraft craft = ResolveCraft(building, required: true);
                RocketClusterDestinationSelector selector = craft.ModuleInterface.GetClusterDestinationSelector();
                if (selector == null)
                {
                    throw new ApiException("unsupported_setting", "Rocket has no ClusterDestinationSelector.");
                }
                AxialI origin = craft.Location;
                AxialI chosen = AxialI.INVALID;
                foreach (AxialI dir in AxialI.DIRECTIONS)
                {
                    AxialI candidate = origin + dir;
                    if (ClusterGrid.Instance.IsValidCell(candidate) &&
                        ClusterGrid.Instance.IsCellVisible(candidate) &&
                        ClusterGrid.Instance.GetAsteroidAtCell(candidate) == null)
                    {
                        chosen = candidate;
                        break;
                    }
                }
                if (chosen == AxialI.INVALID)
                {
                    throw new ApiException("no_orbit_hex", "No visible adjacent empty space hex was found next to the rocket.");
                }
                selector.Repeat = false;
                selector.SetDestination(chosen);
                return new Dictionary<string, object>
                {
                    { "action", "orbit" },
                    { "destination", new[] { chosen.r, chosen.q } }
                };
            }

            if (string.Equals(action, "launch", StringComparison.OrdinalIgnoreCase))
            {
                Clustercraft craft = ResolveCraft(building, required: true);
                if (!craft.ModuleInterface.CheckPreppedForLaunch())
                {
                    List<string> failed = ReadFailedConditions(craft.ModuleInterface);
                    throw new ApiException(
                        "not_prepped_for_launch",
                        "Rocket checklist is incomplete: " + string.Join("; ", failed));
                }
                craft.ModuleInterface.TriggerLaunch();
                return new Dictionary<string, object>
                {
                    { "action", "launch" },
                    { "launch_requested", craft.ModuleInterface.IsLaunchRequested() },
                    { "status", craft.Status.ToString() }
                };
            }

            if (string.Equals(action, "cancel_launch", StringComparison.OrdinalIgnoreCase))
            {
                Clustercraft craft = ResolveCraft(building, required: true);
                craft.ModuleInterface.CancelLaunch();
                return new Dictionary<string, object>
                {
                    { "action", "cancel_launch" },
                    { "launch_requested", craft.ModuleInterface.IsLaunchRequested() }
                };
            }

            if (string.Equals(action, "land", StringComparison.OrdinalIgnoreCase))
            {
                LaunchPad pad = building.GetComponent<LaunchPad>();
                Clustercraft craft = pad != null ? FindOrbitingOrInboundCraft(pad) : ResolveCraft(building, required: true);
                if (craft == null)
                {
                    throw new ApiException("unsupported_setting", "No airborne rocket was found to land.");
                }
                if (pad == null)
                {
                    pad = craft.ModuleInterface.GetClusterDestinationSelector()?.GetDestinationPad();
                }
                if (pad == null)
                {
                    foreach (LaunchPad candidate in Components.LaunchPads.Items)
                    {
                        string unused;
                        if (candidate != null &&
                            candidate.GetComponent<BuildingComplete>() != null &&
                            craft.CanLandAtPad(candidate, out unused) != Clustercraft.PadLandingStatus.CanNeverLand)
                        {
                            pad = candidate;
                            break;
                        }
                    }
                }
                if (pad == null)
                {
                    throw new ApiException("no_landing_pad", "No valid LaunchPad is available for landing.");
                }
                string failReason;
                if (craft.CanLandAtPad(pad, out failReason) == Clustercraft.PadLandingStatus.CanNeverLand)
                {
                    throw new ApiException("cannot_land", UI.StripLinkFormatting(failReason ?? "Cannot land at this LaunchPad."));
                }
                craft.LandAtPad(pad);
                return new Dictionary<string, object>
                {
                    { "action", "land" },
                    { "pad_id", pad.GetComponent<KPrefabID>().InstanceID },
                    { "destination", new[] { pad.GetMyWorldLocation().r, pad.GetMyWorldLocation().q } }
                };
            }

            throw new ApiException("invalid_argument", "rocket_action must be orbit, launch, cancel_launch or land.");
        }

        private static Clustercraft FindOrbitingOrInboundCraft(LaunchPad pad)
        {
            if (pad.LandedRocket?.CraftInterface != null)
            {
                return pad.LandedRocket.CraftInterface.GetComponent<Clustercraft>();
            }
            AxialI padLoc = pad.GetMyWorldLocation();
            return Components.Clustercrafts.Items
                .Where(c => c != null && c.Status != Clustercraft.CraftStatus.Grounded)
                .OrderBy(c => AxialUtil.GetDistance(c.Location, padLoc))
                .FirstOrDefault();
        }

        internal static Dictionary<string, object> DescribeRocket(GameObject go)
        {
            Clustercraft craft = ResolveCraft(go, required: false);
            if (craft == null)
            {
                return null;
            }
            CraftModuleInterface cmi = craft.ModuleInterface;
            WorldContainer interior = cmi.GetInteriorWorld();
            PassengerRocketModule passenger = cmi.GetPassengerModule();
            Dictionary<string, object> info = new Dictionary<string, object>
            {
                { "status", craft.Status.ToString() },
                { "launch_requested", cmi.IsLaunchRequested() },
                { "prepped", cmi.CheckPreppedForLaunch() },
                { "ready_to_launch", cmi.CheckReadyToLaunch() },
                { "location", new[] { craft.Location.r, craft.Location.q } },
                { "destination", new[] { craft.Destination.r, craft.Destination.q } },
                { "range_hexes", cmi.RangeInTiles },
                { "fuel_kg", ApiValues.Number(cmi.FuelRemaining, 1) },
                { "interior_world", interior == null ? (object)null : interior.id },
                { "modules", cmi.ClusterModules
                    .Where(m => m?.Get() != null)
                    .Select(m => m.Get().GetComponent<Building>().Def.PrefabID)
                    .ToList() },
                { "failed_conditions", ReadFailedConditions(cmi) }
            };
            if (passenger != null)
            {
                Tuple<int, int> boarded = passenger.GetCrewBoardedFraction();
                info["crew_boarded"] = boarded.first;
                info["crew_assigned"] = boarded.second;
            }
            if (interior != null)
            {
                List<RocketControlStation> stations = Components.RocketControlStations.GetWorldItems(interior.id);
                if (stations != null && stations.Count > 0)
                {
                    info["restrict_when_grounded"] = stations[0].RestrictWhenGrounded;
                }
            }
            return info;
        }

        private static List<string> ReadFailedConditions(CraftModuleInterface cmi)
        {
            List<string> failed = new List<string>();
            foreach (ProcessCondition.ProcessConditionType condType in RocketConditionTypes)
            {
                List<ProcessCondition> list = new List<ProcessCondition>();
                cmi.PopulateConditionSet(condType, list);
                foreach (ProcessCondition condition in list)
                {
                    if (condition == null)
                    {
                        continue;
                    }
                    ProcessCondition.Status status = condition.EvaluateCondition();
                    if (status == ProcessCondition.Status.Failure)
                    {
                        string msg = UI.StripLinkFormatting(condition.GetStatusMessage(status) ?? condition.GetType().Name);
                        if (!string.IsNullOrWhiteSpace(msg))
                        {
                            failed.Add(msg.Trim());
                        }
                    }
                }
            }
            return failed;
        }
    }
}
