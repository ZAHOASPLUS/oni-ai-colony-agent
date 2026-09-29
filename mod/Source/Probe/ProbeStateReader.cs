using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using HarmonyLib;
using OniAiProbeMod.Contracts;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    internal static class ProbeStateReader
    {
        private const int EnumerationLimit = 10000;
        private const int BuildingSampleLimit = 12;
        private const int TopPrefabLimit = 8;
        private const int BuildingRefreshSeconds = 30;
        private const int DuplicantDetailLimit = 100;
        private const int DuplicantCountLimit = 1000;
        private const int DuplicantPriorityGroupLimit = 256;
        private const int DigCandidateLimit = 32;
        private const int DigDesignationLimit = 32;
        private const int DigTrackedCellLimit = 256;
        private const int DigCandidateRefreshSeconds = 30;
        private const int CandidateScanCellBudget = 4096;
        private const double DigCandidateScanBudgetMs = 8.0;
        // Mirrors CommandFieldContracts.MaxCompletedTechIds (Db.Techs holds about 120).
        private const int CompletedTechLimit = 512;
        private const int TechIdLengthLimit = 128;
        private const int BlueprintCandidateLimit = 32;
        private const int BlueprintTrackedCellLimit = 256;
        private const int BlueprintCandidateRefreshSeconds = 30;
        private const double BlueprintCandidateScanBudgetMs = 10.0;
        // Every id is a catalog key and a per-cell "can_place_<snake>" field
        // (BlueprintCanPlaceField).  2026-09-29: MineralDeoxidizer (Oxygen
        // Diffuser) and AlgaeHabitat (Algae Terrarium), both 1x2 OnFloor
        // buildings, join Ladder/Wire so the model loop can add oxygen
        // production; the game's own placement rules and the research lock
        // decide where they may go.
        private static readonly string[] BlueprintAllowlist =
        {
            "Ladder",
            "Wire",
            "MineralDeoxidizer",
            "AlgaeHabitat"
        };

        private static Dictionary<string, object> cachedBuildings;
        private static int cachedBuildingsWorldId = int.MinValue;
        private static DateTimeOffset nextBuildingsRefreshAt = DateTimeOffset.MinValue;
        // Code review 2026-09-26, L13: these twelve per-building payloads
        // enumerate every completed building in the active world (including
        // world-generated POI walls and props), made up about 78% of each
        // snapshot (about 2.3 MB compact at cycle 6) and about 30 ms of main
        // thread per read, and no decision code consumes them.  They are
        // opt-in diagnostics now: start the game with
        // ONI_AI_EXTENDED_BUILDING_TELEMETRY=1 to publish them again.
        internal const string ExtendedBuildingTelemetryVariable = "ONI_AI_EXTENDED_BUILDING_TELEMETRY";
        internal static readonly string[] ExtendedBuildingTelemetryKeys =
        {
            "functional_building_geometry",
            "functional_building_ports",
            "functional_building_occupancy",
            "functional_building_health",
            "functional_building_flooded",
            "functional_building_reachability",
            "functional_building_power_network",
            "functional_building_utility_network",
            "functional_building_logic_ports",
            "functional_building_utility_endpoint",
            "functional_building_utility_topology",
            "functional_building_utility_contents"
        };
        private static bool extendedBuildingTelemetry = ReadExtendedBuildingTelemetrySwitch();
        private static readonly HashSet<int> TrackedDigCells = new HashSet<int>();
        private static List<int> cachedDigCandidates;
        private static int cachedDigWorldId = int.MinValue;
        private static DateTimeOffset nextDigCandidateRefreshAt = DateTimeOffset.MinValue;
        private static int digCandidateScanOffset;
        private static readonly HashSet<int> TrackedBlueprintCells = new HashSet<int>();
        private static readonly Dictionary<string, UtilityPathRecord> UtilityPaths =
            new Dictionary<string, UtilityPathRecord>(StringComparer.Ordinal);
        private static Dictionary<string, List<int>> cachedBlueprintCandidates;
        private static int cachedBlueprintWorldId = int.MinValue;
        private static DateTimeOffset nextBlueprintCandidateRefreshAt = DateTimeOffset.MinValue;
        private static int blueprintCandidateScanOffset;

        internal static void ResetSession()
        {
            extendedBuildingTelemetry = ReadExtendedBuildingTelemetrySwitch();
            cachedBuildings = null;
            cachedBuildingsWorldId = int.MinValue;
            nextBuildingsRefreshAt = DateTimeOffset.MinValue;
            TrackedDigCells.Clear();
            cachedDigCandidates = null;
            cachedDigWorldId = int.MinValue;
            nextDigCandidateRefreshAt = DateTimeOffset.MinValue;
            digCandidateScanOffset = 0;
            TrackedBlueprintCells.Clear();
            UtilityPaths.Clear();
            cachedBlueprintCandidates = null;
            cachedBlueprintWorldId = int.MinValue;
            nextBlueprintCandidateRefreshAt = DateTimeOffset.MinValue;
            blueprintCandidateScanOffset = 0;
            SurvivalTelemetryReader.ResetSession();
        }

        private static bool ReadExtendedBuildingTelemetrySwitch()
        {
            try
            {
                return string.Equals(
                    Environment.GetEnvironmentVariable(ExtendedBuildingTelemetryVariable),
                    "1",
                    StringComparison.Ordinal);
            }
            catch
            {
                return false;
            }
        }

        internal static void TrackDigCell(int cell)
        {
            if (TrackedDigCells.Contains(cell) || TrackedDigCells.Count < DigTrackedCellLimit)
            {
                TrackedDigCells.Add(cell);
            }
        }

        internal static void TrackBlueprintCell(int cell)
        {
            if (TrackedBlueprintCells.Contains(cell) || TrackedBlueprintCells.Count < BlueprintTrackedCellLimit)
            {
                TrackedBlueprintCells.Add(cell);
            }
        }

        internal static void RegisterUtilityPath(
            string pathId,
            string buildingDefId,
            IList<int> cells,
            IList<GameObject> blueprints)
        {
            UtilityPathRecord record = new UtilityPathRecord
            {
                PathId = pathId,
                BuildingDefId = buildingDefId
            };
            for (int index = 0; index < cells.Count && index < blueprints.Count; index++)
            {
                GameObject blueprint = blueprints[index];
                KPrefabID prefab = blueprint == null ? null : blueprint.GetComponent<KPrefabID>();
                record.Targets.Add(new BlueprintTargetRecord
                {
                    Cell = cells[index],
                    InstanceId = prefab == null ? 0 : prefab.InstanceID,
                    Blueprint = blueprint
                });
                TrackBlueprintCell(cells[index]);
            }
            UtilityPaths[pathId] = record;
        }

        internal static bool UtilityPathExists(string pathId)
        {
            return !string.IsNullOrEmpty(pathId) && UtilityPaths.ContainsKey(pathId);
        }

        internal static bool UtilityPathMatches(
            string pathId,
            string buildingDefId,
            IList<int> cells,
            IList<int> instanceIds)
        {
            if (!UtilityPaths.TryGetValue(pathId, out UtilityPathRecord record) ||
                !string.Equals(record.BuildingDefId, buildingDefId, StringComparison.Ordinal) ||
                record.Targets.Count != cells.Count ||
                cells.Count != instanceIds.Count)
            {
                return false;
            }
            for (int index = 0; index < record.Targets.Count; index++)
            {
                BlueprintTargetRecord target = record.Targets[index];
                RefreshBlueprintTargetIdentity(target);
                if (target.Cell != cells[index] || target.InstanceId != instanceIds[index])
                {
                    return false;
                }
            }
            return true;
        }

        internal static Dictionary<string, object> ReadState()
        {
            Dictionary<string, object> metrics = new Dictionary<string, object>();
            Dictionary<string, object> game = Timed("game", ReadGame, metrics);
            object cycle = Timed("cycle", ReadCycle, metrics);
            object gameTimeCycles = Timed("game_time_cycles", ReadGameTimeCycles, metrics);
            Dictionary<string, object> duplicants = Timed("duplicants", ReadDuplicants, metrics);
            Dictionary<string, object> resources = Timed("resources", ReadResources, metrics);
            SurvivalTelemetryReadResult survival = Timed("survival", SurvivalTelemetryReader.Read, metrics);
            ReportTelemetryReadResult reportTelemetry = Timed(
                "report_counters",
                ReportTelemetryReader.Read,
                metrics);
            CanonicalAlertReadResult alertResult = Timed(
                "alerts",
                CanonicalAlertReader.Read,
                metrics);
            if (alertResult == null)
            {
                alertResult = CanonicalAlertReadResult.Error(
                    CanonicalAlertReader.Source,
                    "alert_result_missing");
            }
            Dictionary<string, object> research = Timed("research", ReadResearch, metrics);
            Dictionary<string, object> buildings = Timed("buildings", ReadBuildings, metrics);
            Dictionary<string, object> functionalBuildings = Timed(
                "functional_buildings",
                FunctionalBuildingTelemetryReader.Read,
                metrics);
            bool extended = extendedBuildingTelemetry;
            Dictionary<string, object> functionalBuildingGeometry = extended
                ? Timed("functional_building_geometry", FunctionalBuildingGeometryTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingPorts = extended
                ? Timed("functional_building_ports", FunctionalBuildingPortsTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingOccupancy = extended
                ? Timed("functional_building_occupancy", FunctionalBuildingOccupancyTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingHealth = extended
                ? Timed("functional_building_health", FunctionalBuildingHealthTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingFlooded = extended
                ? Timed("functional_building_flooded", FunctionalBuildingFloodedTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingReachability = extended
                ? Timed("functional_building_reachability", FunctionalBuildingReachabilityTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingPowerNetwork = extended
                ? Timed("functional_building_power_network", FunctionalBuildingPowerNetworkTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingUtilityNetwork = extended
                ? Timed("functional_building_utility_network", FunctionalBuildingUtilityNetworkTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingLogicPorts = extended
                ? Timed("functional_building_logic_ports", FunctionalBuildingLogicPortsTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingUtilityEndpoint = extended
                ? Timed("functional_building_utility_endpoint", FunctionalBuildingUtilityEndpointTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingUtilityTopology = extended
                ? Timed("functional_building_utility_topology", FunctionalBuildingUtilityTopologyTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> functionalBuildingUtilityContents = extended
                ? Timed("functional_building_utility_contents", FunctionalBuildingUtilityContentsTelemetryReader.Read, metrics)
                : null;
            Dictionary<string, object> designations = Timed("designations", ReadDesignations, metrics);
            BlueprintReadResult blueprintState = Timed("blueprints", ReadBlueprintState, metrics);
            Dictionary<string, object> quality = BuildQuality(
                game,
                cycle,
                duplicants,
                resources,
                alertResult,
                research,
                buildings,
                functionalBuildings,
                functionalBuildingGeometry,
                functionalBuildingPorts,
                functionalBuildingOccupancy,
                functionalBuildingHealth,
                functionalBuildingFlooded,
                functionalBuildingReachability,
                functionalBuildingPowerNetwork,
                functionalBuildingUtilityNetwork,
                functionalBuildingLogicPorts,
                functionalBuildingUtilityEndpoint,
                functionalBuildingUtilityTopology,
                functionalBuildingUtilityContents,
                designations,
                blueprintState);
            foreach (KeyValuePair<string, object> entry in survival.Quality)
            {
                quality[entry.Key] = entry.Value;
            }
            foreach (KeyValuePair<string, object> entry in reportTelemetry.Quality)
            {
                quality[entry.Key] = entry.Value;
            }
            quality["game_time_cycles"] = Quality(
                gameTimeCycles == null ? "error" : "observed",
                "GameClock.GetTimeInCycles",
                "display_1_based_continuous",
                "cycle",
                "save",
                gameTimeCycles == null ? "unknown" : "complete");

            game.Remove("_paused_status");
            game.Remove("_paused_source");
            game.Remove("_build_mode_status");
            duplicants.Remove("_details_status");
            duplicants.Remove("_details_source");
            duplicants.Remove("_total_status");
            duplicants.Remove("_total_source");
            duplicants.Remove("_idle_status");
            duplicants.Remove("_idle_source");
            duplicants.Remove("_stressed_status");
            duplicants.Remove("_stressed_source");
            resources.Remove("_food_status");
            resources.Remove("_food_source");
            resources.Remove("_food_completeness");
            research.Remove("_current_status");
            research.Remove("_current_source");
            research.Remove("_complete_status");
            research.Remove("_complete_source");
            functionalBuildings.Remove("_status");
            functionalBuildings.Remove("_source");
            functionalBuildings.Remove("_reason");
            functionalBuildingGeometry?.Remove("_status");
            functionalBuildingGeometry?.Remove("_source");
            functionalBuildingGeometry?.Remove("_reason");
            functionalBuildingPorts?.Remove("_status");
            functionalBuildingPorts?.Remove("_source");
            functionalBuildingPorts?.Remove("_reason");
            functionalBuildingOccupancy?.Remove("_status");
            functionalBuildingOccupancy?.Remove("_source");
            functionalBuildingOccupancy?.Remove("_reason");
            functionalBuildingHealth?.Remove("_status");
            functionalBuildingHealth?.Remove("_source");
            functionalBuildingHealth?.Remove("_reason");
            functionalBuildingFlooded?.Remove("_status");
            functionalBuildingFlooded?.Remove("_source");
            functionalBuildingFlooded?.Remove("_reason");
            functionalBuildingReachability?.Remove("_status");
            functionalBuildingReachability?.Remove("_source");
            functionalBuildingReachability?.Remove("_reason");
            functionalBuildingPowerNetwork?.Remove("_status");
            functionalBuildingPowerNetwork?.Remove("_source");
            functionalBuildingPowerNetwork?.Remove("_reason");
            functionalBuildingUtilityNetwork?.Remove("_status");
            functionalBuildingUtilityNetwork?.Remove("_source");
            functionalBuildingUtilityNetwork?.Remove("_reason");
            functionalBuildingLogicPorts?.Remove("_status");
            functionalBuildingLogicPorts?.Remove("_source");
            functionalBuildingLogicPorts?.Remove("_reason");
            functionalBuildingUtilityEndpoint?.Remove("_status");
            functionalBuildingUtilityEndpoint?.Remove("_source");
            functionalBuildingUtilityEndpoint?.Remove("_reason");
            functionalBuildingUtilityTopology?.Remove("_status");
            functionalBuildingUtilityTopology?.Remove("_source");
            functionalBuildingUtilityTopology?.Remove("_reason");
            functionalBuildingUtilityContents?.Remove("_status");
            functionalBuildingUtilityContents?.Remove("_source");
            functionalBuildingUtilityContents?.Remove("_reason");
            Dictionary<string, object> dig = designations["dig"] as Dictionary<string, object>;
            dig?.Remove("_status");
            dig?.Remove("_tool_status");
            dig?.Remove("_source");

            Dictionary<string, object> state = new Dictionary<string, object>
            {
                { "schema", "oni.state.v2" },
                { "event", "state_snapshot" },
                { "game", game },
                { "cycle", cycle },
                { "game_time_cycles", gameTimeCycles },
                { "duplicants", duplicants },
                { "resources", resources },
                { "report_counters", reportTelemetry.Counters },
                { "observation_scope", survival.ObservationScope },
                { "materials", survival.Materials },
                { "production", survival.Production },
                { "consumption", survival.Consumption },
                { "power", survival.Power },
                { "oxygen", survival.Oxygen },
                { "water", survival.Water },
                { "temperature", survival.Temperature },
                { "rockets", survival.Rockets },
                { "space", survival.Space },
                { "resilience", survival.Resilience },
                { "colony", survival.Colony },
                { "alerts", alertResult.Value },
                { "alerts_by_world", alertResult.WorldsValue },
                { "research", research },
                { "buildings", buildings },
                { "functional_buildings", functionalBuildings },
                { "designations", designations },
                { "blueprints", blueprintState.Blueprints },
                { "cells", blueprintState.Cells },
                { "quality", quality },
                { "read_metrics_ms", metrics },
                // P0-1: lets tooling assert that a validation session cannot autosave.
                { "autosave", AutosaveGuard.Describe() }
            };
            Dictionary<string, object>[] extendedPayloads =
            {
                functionalBuildingGeometry,
                functionalBuildingPorts,
                functionalBuildingOccupancy,
                functionalBuildingHealth,
                functionalBuildingFlooded,
                functionalBuildingReachability,
                functionalBuildingPowerNetwork,
                functionalBuildingUtilityNetwork,
                functionalBuildingLogicPorts,
                functionalBuildingUtilityEndpoint,
                functionalBuildingUtilityTopology,
                functionalBuildingUtilityContents
            };
            for (int index = 0; index < ExtendedBuildingTelemetryKeys.Length; index++)
            {
                if (extended)
                {
                    state[ExtendedBuildingTelemetryKeys[index]] = extendedPayloads[index];
                }
                else
                {
                    quality.Remove(ExtendedBuildingTelemetryKeys[index]);
                }
            }
            return state;
        }

        private static Dictionary<string, object> ReadGame()
        {
            object paused = null;
            object sandboxMode = null;
            object instantBuildMode = null;
            string status = "unsupported";
            string buildModeStatus = "unsupported";
            string source = "SpeedControlScreen.IsPaused";
            try
            {
                if (SpeedControlScreen.Instance != null)
                {
                    paused = SpeedControlScreen.Instance.IsPaused;
                    status = "observed";
                }
                if (Game.Instance != null)
                {
                    sandboxMode = Game.Instance.SandboxModeActive;
                    instantBuildMode = DebugHandler.InstantBuildMode;
                    buildModeStatus = "observed";
                }
            }
            catch
            {
                status = "error";
                buildModeStatus = "error";
            }

            return new Dictionary<string, object>
            {
                { "paused", paused },
                { "sandbox_mode", sandboxMode },
                { "instant_build_mode", instantBuildMode },
                { "_paused_status", status },
                { "_paused_source", source },
                { "_build_mode_status", buildModeStatus }
            };
        }

        private static object ReadCycle()
        {
            try
            {
                return GameClock.Instance == null ? null : (object)(GameClock.Instance.GetCycle() + 1);
            }
            catch
            {
                return null;
            }
        }

        private static object ReadGameTimeCycles()
        {
            try
            {
                return GameClock.Instance == null
                    ? null
                    : (object)Math.Round(GameClock.Instance.GetTimeInCycles() + 1.0, 6);
            }
            catch
            {
                return null;
            }
        }

        private static Dictionary<string, object> ReadDuplicants()
        {
            DuplicantCountReadResult counts = ReadActiveWorldDuplicantCounts();
            string detailsStatus;
            string detailsSource;
            Dictionary<string, object> byInstanceId = ReadDuplicantPriorityState(out detailsStatus, out detailsSource);
            return new Dictionary<string, object>
            {
                { "total", counts.Total },
                { "stressed", counts.Stressed },
                { "idle", counts.Idle },
                { "by_instance_id", byInstanceId },
                { "_total_status", counts.TotalStatus },
                { "_total_source", CommandFieldContracts.ActiveWorldDuplicantCountSource },
                { "_idle_status", counts.IdleStatus },
                { "_idle_source", CommandFieldContracts.DuplicantIdleSource },
                { "_stressed_status", counts.StressedStatus },
                { "_stressed_source", CommandFieldContracts.DuplicantStressedSource },
                { "_details_status", detailsStatus },
                { "_details_source", detailsSource }
            };
        }

        private static DuplicantCountReadResult ReadActiveWorldDuplicantCounts()
        {
            DuplicantCountReadResult result = new DuplicantCountReadResult();
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return result;
                }

                WorldContainer activeWorld = manager.activeWorld;
                int activeWorldId = manager.activeWorldId;
                if (activeWorld == null || activeWorldId < 0)
                {
                    return result;
                }
                if (!DuplicantWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    result.MarkEnumerationError();
                    return result;
                }

                IEnumerable<MinionIdentity> identities = Components.LiveMinionIdentities.GetWorldItems(
                    activeWorldId,
                    checkChildWorlds: false);
                if (identities == null)
                {
                    result.MarkEnumerationError();
                    return result;
                }

                HashSet<int> instanceIds = new HashSet<int>();
                int idleCount = 0;
                int stressedCount = 0;
                bool idleReliable = true;
                bool stressedReliable = true;
                foreach (MinionIdentity identity in identities)
                {
                    // Count == limit is valid only when enumeration ends here. Seeing
                    // another item proves the bounded snapshot is incomplete.
                    if (instanceIds.Count >= DuplicantCountLimit)
                    {
                        result.MarkEnumerationError();
                        return result;
                    }

                    // UnityEngine.Object equality also treats destroyed wrappers as null.
                    if (identity == null)
                    {
                        result.MarkEnumerationError();
                        return result;
                    }

                    if (identity.GetMyWorldId() != activeWorldId)
                    {
                        result.MarkEnumerationError();
                        return result;
                    }

                    KPrefabID prefab = identity.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !instanceIds.Add(prefab.InstanceID))
                    {
                        result.MarkEnumerationError();
                        return result;
                    }

                    if (idleReliable)
                    {
                        try
                        {
                            if (prefab.HasTag(GameTags.Idle))
                            {
                                idleCount++;
                            }
                        }
                        catch
                        {
                            idleReliable = false;
                        }
                    }

                    if (stressedReliable)
                    {
                        try
                        {
                            StressMonitor.Instance monitor = identity.GetSMI<StressMonitor.Instance>();
                            if (monitor == null)
                            {
                                stressedReliable = false;
                            }
                            else if (monitor.IsStressed())
                            {
                                stressedCount++;
                            }
                        }
                        catch
                        {
                            stressedReliable = false;
                        }
                    }
                }

                // The world can change while a snapshot is being read. Never
                // publish counts unless both the manager and active-world object
                // remain the exact instances captured before enumeration.
                if (!DuplicantWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    result.MarkEnumerationError();
                    return result;
                }

                result.Total = instanceIds.Count;
                result.TotalStatus = "observed";
                result.Idle = idleReliable ? (int?)idleCount : null;
                result.IdleStatus = idleReliable ? "observed" : "error";
                result.Stressed = stressedReliable ? (int?)stressedCount : null;
                result.StressedStatus = stressedReliable ? "observed" : "error";
                return result;
            }
            catch
            {
                result.MarkEnumerationError();
                return result;
            }
        }

        private static bool DuplicantWorldBindingMatches(
            ClusterManager manager,
            WorldContainer activeWorld,
            int activeWorldId)
        {
            return manager != null &&
                activeWorld != null &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId &&
                activeWorld.id == activeWorldId;
        }

        private static Dictionary<string, object> ReadDuplicantPriorityState(out string status, out string source)
        {
            const string typedSource = "Components.LiveMinionIdentities+ChoreConsumer";
            Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.Ordinal);
            status = "unsupported";
            source = typedSource;
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return result;
                }

                WorldContainer activeWorld = manager.activeWorld;
                int activeWorldId = manager.activeWorldId;
                Db database = Db.Get();
                var choreGroups = database?.ChoreGroups;
                IEnumerable<ChoreGroup> choreGroupResources = choreGroups?.resources;
                if (activeWorld == null || activeWorldId < 0 ||
                    database == null || choreGroups == null || choreGroupResources == null)
                {
                    return result;
                }
                if (!DuplicantWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    status = "error";
                    return result;
                }
                if (!DuplicantPriorityBindingsMatch(database, choreGroups, choreGroupResources))
                {
                    status = "error";
                    return result;
                }

                List<ChoreGroup> priorityGroups = new List<ChoreGroup>();
                HashSet<string> choreGroupIds = new HashSet<string>(StringComparer.Ordinal);
                int enumeratedGroupCount = 0;
                foreach (ChoreGroup group in choreGroupResources)
                {
                    if (enumeratedGroupCount >= DuplicantPriorityGroupLimit)
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }
                    enumeratedGroupCount++;

                    if (group == null || string.IsNullOrWhiteSpace(group.Id) ||
                        !choreGroupIds.Add(group.Id))
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }
                    if (group.userPrioritizable)
                    {
                        priorityGroups.Add(group);
                    }
                }
                if (enumeratedGroupCount == 0 || priorityGroups.Count == 0 ||
                    !DuplicantPriorityBindingsMatch(database, choreGroups, choreGroupResources))
                {
                    status = "error";
                    result.Clear();
                    return result;
                }

                IEnumerable<MinionIdentity> identities = Components.LiveMinionIdentities.GetWorldItems(
                    activeWorldId,
                    checkChildWorlds: false);
                if (identities == null)
                {
                    status = "error";
                    return result;
                }

                int scanned = 0;
                foreach (MinionIdentity identity in identities)
                {
                    if (scanned >= DuplicantDetailLimit)
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }

                    if (identity == null)
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }

                    int identityWorldId = identity.GetMyWorldId();
                    if (identityWorldId != activeWorldId)
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }

                    KPrefabID prefab = identity.GetComponent<KPrefabID>();
                    ChoreConsumer consumer = identity.GetComponent<ChoreConsumer>();
                    if (prefab == null || prefab.InstanceID <= 0 || consumer == null)
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }

                    Dictionary<string, object> priorities = new Dictionary<string, object>(StringComparer.Ordinal);
                    foreach (ChoreGroup group in priorityGroups)
                    {
                        priorities.Add(group.Id, consumer.GetPersonalPriority(group));
                    }

                    string key = DuplicantStateKey(prefab.InstanceID);
                    if (result.ContainsKey(key))
                    {
                        status = "error";
                        result.Clear();
                        return result;
                    }
                    result[key] = new Dictionary<string, object>
                    {
                        { "instance_id", prefab.InstanceID },
                        { "name", identity.GetProperName() },
                        { "world_id", identityWorldId },
                        { "chore_priorities", priorities }
                    };
                    scanned++;
                }

                if (!DuplicantWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    status = "error";
                    result.Clear();
                    return result;
                }
                if (!DuplicantPriorityBindingsMatch(database, choreGroups, choreGroupResources))
                {
                    status = "error";
                    result.Clear();
                    return result;
                }

                status = "observed";
                return result;
            }
            catch
            {
                status = "error";
                result.Clear();
                return result;
            }
        }

        private static bool DuplicantPriorityBindingsMatch(
            Db database,
            object choreGroups,
            object choreGroupResources)
        {
            return database != null &&
                choreGroups != null &&
                choreGroupResources != null &&
                ReferenceEquals(Db.Get(), database) &&
                ReferenceEquals(database.ChoreGroups, choreGroups) &&
                ReferenceEquals(database.ChoreGroups.resources, choreGroupResources);
        }

        internal static string DuplicantStateKey(int instanceId)
        {
            return "id_" + instanceId.ToString(CultureInfo.InvariantCulture);
        }

        private static Dictionary<string, object> ReadDesignations()
        {
            string status;
            string toolStatus;
            string source;
            Dictionary<string, object> dig = ReadDigState(out status, out toolStatus, out source);
            dig["_status"] = status;
            dig["_tool_status"] = toolStatus;
            dig["_source"] = source;
            return new Dictionary<string, object>
            {
                { "dig", dig }
            };
        }

        private static Dictionary<string, object> ReadDigState(
            out string status,
            out string toolStatus,
            out string source)
        {
            const string typedSource = "Grid+DigTool+Components.Diggables";
            status = "unsupported";
            toolStatus = "unsupported";
            source = typedSource;
            Dictionary<string, object> byCell = new Dictionary<string, object>(StringComparer.Ordinal);
            List<object> candidateCells = new List<object>();
            Dictionary<string, object> tool = new Dictionary<string, object>
            {
                { "available", false },
                { "tiles_filter_on", null },
                { "natural_backwall_filter_on", null }
            };

            try
            {
                int activeWorldId = ReadActiveWorldId();
                if (activeWorldId < 0 || ClusterManager.Instance == null)
                {
                    return DigState(tool, candidateCells, byCell);
                }

                DigTool digTool = DigTool.Instance;
                tool["available"] = digTool != null;
                if (digTool != null)
                {
                    tool["tiles_filter_on"] = digTool.IsActiveLayer(ToolParameterMenu.FILTERLAYERS.TILES);
                    tool["natural_backwall_filter_on"] = digTool.IsActiveLayer(
                        ToolParameterMenu.FILTERLAYERS.NATURALBACKWALL);
                    toolStatus = "observed";
                }

                RefreshDigCandidates(activeWorldId);
                HashSet<int> cells = new HashSet<int>();
                if (cachedDigCandidates != null)
                {
                    foreach (int cell in cachedDigCandidates)
                    {
                        cells.Add(cell);
                    }
                }

                int designationCount = 0;
                IEnumerable<Diggable> diggables = Components.Diggables.GetWorldItems(
                    activeWorldId,
                    checkChildWorlds: false);
                if (diggables != null)
                {
                    foreach (Diggable diggable in diggables)
                    {
                        if (designationCount >= DigDesignationLimit)
                        {
                            break;
                        }

                        if (diggable == null)
                        {
                            continue;
                        }

                        int cell = diggable.GetCell();
                        if (Grid.IsValidCellInWorld(cell, activeWorldId))
                        {
                            cells.Add(cell);
                            designationCount++;
                        }
                    }
                }

                foreach (int cell in TrackedDigCells)
                {
                    if (Grid.IsValidCellInWorld(cell, activeWorldId))
                    {
                        cells.Add(cell);
                    }
                }

                List<int> orderedCells = new List<int>(cells);
                orderedCells.Sort();
                foreach (int cell in orderedCells)
                {
                    Dictionary<string, object> details = ReadDigCell(cell, activeWorldId);
                    if (details == null)
                    {
                        continue;
                    }

                    byCell[DigCellKey(cell)] = details;
                    if (details["safe_mark_candidate"] is bool safe && safe && candidateCells.Count < DigCandidateLimit)
                    {
                        candidateCells.Add(cell);
                    }
                }

                status = "observed";
                return DigState(tool, candidateCells, byCell);
            }
            catch
            {
                status = "error";
                byCell.Clear();
                candidateCells.Clear();
                return DigState(tool, candidateCells, byCell);
            }
        }

        private static Dictionary<string, object> DigState(
            Dictionary<string, object> tool,
            List<object> candidateCells,
            Dictionary<string, object> byCell)
        {
            return new Dictionary<string, object>
            {
                { "tool", tool },
                { "candidate_cells", candidateCells },
                { "by_cell", byCell }
            };
        }

        private static void RefreshDigCandidates(int worldId)
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            if (cachedDigCandidates != null && cachedDigWorldId == worldId && now < nextDigCandidateRefreshAt)
            {
                return;
            }

            WorldContainer world = ClusterManager.Instance.GetWorld(worldId);
            if (world == null)
            {
                cachedDigCandidates = new List<int>();
                cachedDigWorldId = worldId;
                nextDigCandidateRefreshAt = now.AddSeconds(DigCandidateRefreshSeconds);
                return;
            }
            int minX;
            int minY;
            int maxX;
            int maxY;
            GetActiveWorldBounds(world, out minX, out minY, out maxX, out maxY);
            int width = maxX - minX + 1;
            int height = maxY - minY + 1;
            int totalCells = Math.Max(0, width * height);
            bool sameWorld = cachedDigCandidates != null && cachedDigWorldId == worldId;
            if (!sameWorld)
            {
                cachedDigCandidates = new List<int>();
                cachedDigWorldId = worldId;
                int centerX = (minX + maxX) / 2;
                int centerY = (minY + maxY) / 2;
                digCandidateScanOffset = Math.Max(0, (centerY - minY) * width + centerX - minX);
            }
            else
            {
                cachedDigCandidates.RemoveAll(cell => !IsSafeDigMarkCandidate(cell, worldId));
            }

            HashSet<int> existing = new HashSet<int>(cachedDigCandidates);
            Stopwatch budget = Stopwatch.StartNew();
            int checkedCells = 0;
            while (totalCells > 0 &&
                   checkedCells < CandidateScanCellBudget &&
                   cachedDigCandidates.Count < DigCandidateLimit &&
                   budget.Elapsed.TotalMilliseconds < DigCandidateScanBudgetMs)
            {
                int offset = digCandidateScanOffset % totalCells;
                digCandidateScanOffset = (digCandidateScanOffset + 1) % totalCells;
                checkedCells++;
                int x = minX + offset % width;
                int y = minY + offset / width;
                int cell = Grid.XYToCell(x, y);
                if (existing.Add(cell) && IsSafeDigMarkCandidate(cell, worldId))
                {
                    cachedDigCandidates.Add(cell);
                }
            }
            nextDigCandidateRefreshAt = now.AddSeconds(
                cachedDigCandidates.Count >= DigCandidateLimit ? DigCandidateRefreshSeconds : 1);
        }

        internal static bool IsSafeDigMarkCandidate(int cell, int worldId)
        {
            if (!Grid.IsValidCellInWorld(cell, worldId) ||
                !Grid.IsVisible(cell) ||
                !Grid.Solid[cell] ||
                Grid.Foundation[cell] ||
                Grid.Objects[cell, 7] != null ||
                HasConstructable(cell))
            {
                return false;
            }

            Element element = Grid.Element[cell];
            return element != null && element.IsSolid && element.hardness != byte.MaxValue;
        }

        internal static bool HasConstructable(int cell)
        {
            for (int layer = 0; layer < 45; layer++)
            {
                GameObject gameObject = Grid.Objects[cell, layer];
                if (gameObject != null && gameObject.GetComponent<Constructable>() != null)
                {
                    return true;
                }
            }

            return false;
        }

        private static Dictionary<string, object> ReadDigCell(int cell, int worldId)
        {
            if (!Grid.IsValidCellInWorld(cell, worldId))
            {
                return null;
            }

            Vector2I xy = Grid.CellToXY(cell);
            Element element = Grid.Element[cell];
            Diggable designation = Diggable.GetDiggable(cell);
            KPrefabID prefab = designation?.GetComponent<KPrefabID>();
            int designationInstanceId = prefab == null ? 0 : prefab.InstanceID;
            string designationKind = DigDesignationKind(designation);
            bool designationInProgress = designation?.chore != null && designation.chore.InProgress();
            bool hasConstructable = HasConstructable(cell);
            bool safeMarkCandidate = IsSafeDigMarkCandidate(cell, worldId);
            string elementId = element?.tag.Name ?? "unknown";
            float mass = Grid.Mass[cell];
            int hardness = element == null ? -1 : element.hardness;
            float damage = Grid.Damage[cell];
            string fingerprint = DigCellFingerprint(
                cell,
                worldId,
                Grid.IsVisible(cell),
                Grid.Solid[cell],
                Grid.Foundation[cell],
                elementId,
                mass,
                hardness,
                damage,
                hasConstructable,
                designationInstanceId,
                designationKind,
                designationInProgress);

            return new Dictionary<string, object>
            {
                { "cell", cell },
                { "x", xy.x },
                { "y", xy.y },
                { "world_id", worldId },
                { "visible", Grid.IsVisible(cell) },
                { "solid", Grid.Solid[cell] },
                { "foundation", Grid.Foundation[cell] },
                { "element_id", elementId },
                { "mass_kg", mass },
                { "hardness", hardness },
                { "damage", damage },
                { "has_constructable", hasConstructable },
                { "designation_present", designation != null },
                { "designation_instance_id", designationInstanceId },
                { "designation_kind", designationKind },
                { "designation_in_progress", designationInProgress },
                { "safe_mark_candidate", safeMarkCandidate },
                { "fingerprint", fingerprint }
            };
        }

        private static string DigDesignationKind(Diggable designation)
        {
            if (designation == null)
            {
                return "none";
            }

            bool tile = designation.WillDigTile();
            bool backwall = designation.WillDigBackwall();
            if (tile && backwall)
            {
                return "tile+backwall";
            }

            return tile ? "tile" : backwall ? "backwall" : "none";
        }

        private static string DigCellFingerprint(
            int cell,
            int worldId,
            bool visible,
            bool solid,
            bool foundation,
            string elementId,
            float mass,
            int hardness,
            float damage,
            bool hasConstructable,
            int designationInstanceId,
            string designationKind,
            bool designationInProgress)
        {
            string canonical = string.Join(
                "|",
                cell.ToString(CultureInfo.InvariantCulture),
                worldId.ToString(CultureInfo.InvariantCulture),
                visible ? "1" : "0",
                solid ? "1" : "0",
                foundation ? "1" : "0",
                elementId ?? "unknown",
                mass.ToString("R", CultureInfo.InvariantCulture),
                hardness.ToString(CultureInfo.InvariantCulture),
                damage.ToString("R", CultureInfo.InvariantCulture),
                hasConstructable ? "1" : "0",
                designationInstanceId.ToString(CultureInfo.InvariantCulture),
                designationKind ?? "none",
                designationInProgress ? "1" : "0");
            using (SHA256 sha256 = SHA256.Create())
            {
                byte[] digest = sha256.ComputeHash(Encoding.UTF8.GetBytes(canonical));
                return "sha256:" + BitConverter.ToString(digest).Replace("-", string.Empty).ToLowerInvariant();
            }
        }

        internal static string DigCellKey(int cell)
        {
            return "cell_" + cell.ToString(CultureInfo.InvariantCulture);
        }

        private static BlueprintReadResult ReadBlueprintState()
        {
            BlueprintReadResult result = new BlueprintReadResult();
            try
            {
                int worldId = ReadActiveWorldId();
                WorldContainer world = worldId < 0 || ClusterManager.Instance == null
                    ? null
                    : ClusterManager.Instance.GetWorld(worldId);
                if (world == null)
                {
                    return result;
                }

                RefreshBlueprintCandidates(worldId);
                Dictionary<string, object> catalog = new Dictionary<string, object>(StringComparer.Ordinal);
                Dictionary<string, List<Tag>> selectedMaterialsByDef =
                    new Dictionary<string, List<Tag>>(StringComparer.Ordinal);
                HashSet<int> cells = new HashSet<int>(TrackedBlueprintCells);
                foreach (string defId in BlueprintAllowlist)
                {
                    BuildingDef def = Assets.GetBuildingDef(defId);
                    List<Tag> selectedMaterials;
                    List<float> availableMasses;
                    bool materialsAvailable;
                    TrySelectBlueprintMaterials(
                        def,
                        world,
                        out selectedMaterials,
                        out availableMasses,
                        out materialsAvailable);
                    selectedMaterialsByDef[defId] = selectedMaterials;
                    List<int> candidates = cachedBlueprintCandidates != null &&
                        cachedBlueprintCandidates.TryGetValue(defId, out List<int> cached)
                        ? cached
                        : new List<int>();
                    foreach (int cell in candidates)
                    {
                        cells.Add(cell);
                    }
                    catalog[defId] = ReadBlueprintCatalogEntry(
                        def,
                        selectedMaterials,
                        availableMasses,
                        materialsAvailable,
                        candidates);
                }

                foreach (UtilityPathRecord path in UtilityPaths.Values)
                {
                    foreach (BlueprintTargetRecord target in path.Targets)
                    {
                        cells.Add(target.Cell);
                    }
                }

                Dictionary<string, object> byCell = new Dictionary<string, object>(StringComparer.Ordinal);
                Dictionary<string, object> byInstanceId = new Dictionary<string, object>(StringComparer.Ordinal);
                List<int> orderedCells = new List<int>(cells);
                orderedCells.Sort();
                foreach (int cell in orderedCells)
                {
                    if (!Grid.IsValidCellInWorld(cell, worldId))
                    {
                        continue;
                    }
                    Dictionary<string, object> details = ReadBlueprintCell(
                        cell,
                        worldId,
                        selectedMaterialsByDef);
                    byCell[DigCellKey(cell)] = details;
                    if (details["blueprint_instance_id"] is int instanceId && instanceId > 0)
                    {
                        byInstanceId["id_" + instanceId.ToString(CultureInfo.InvariantCulture)] =
                            ReadBlueprintIdentity(details);
                    }
                }

                result.Blueprints["catalog"] = catalog;
                result.Blueprints["by_instance_id"] = byInstanceId;
                result.Blueprints["utility_paths"] = ReadUtilityPathState();
                result.Cells["by_id"] = byCell;
                result.Status = "observed";
                result.Source = "BuildingDef+Grid+Constructable+WorldInventory";
                return result;
            }
            catch
            {
                result.Blueprints["catalog"] = new Dictionary<string, object>();
                result.Blueprints["by_instance_id"] = new Dictionary<string, object>();
                result.Blueprints["utility_paths"] = new Dictionary<string, object>();
                result.Cells["by_id"] = new Dictionary<string, object>();
                result.Status = "error";
                return result;
            }
        }

        private static Dictionary<string, object> ReadBlueprintCatalogEntry(
            BuildingDef def,
            List<Tag> selectedMaterials,
            List<float> availableMasses,
            bool materialsAvailable,
            List<int> candidates)
        {
            List<object> materialTags = new List<object>();
            foreach (Tag tag in selectedMaterials)
            {
                materialTags.Add(tag.Name);
            }
            List<object> available = new List<object>();
            foreach (float mass in availableMasses)
            {
                available.Add(mass);
            }
            List<object> required = new List<object>();
            List<object> categories = new List<object>();
            if (def?.CraftRecipe?.Ingredients != null)
            {
                foreach (Recipe.Ingredient ingredient in def.CraftRecipe.Ingredients)
                {
                    categories.Add(ingredient.tag.Name);
                    required.Add(ingredient.amount);
                }
            }
            List<object> candidateCells = new List<object>();
            foreach (int cell in candidates)
            {
                candidateCells.Add(cell);
            }
            return new Dictionary<string, object>
            {
                { "available", def != null },
                { "prefab_id", def?.PrefabID },
                { "is_utility", def?.isUtility ?? false },
                { "cancellable", def?.Cancellable ?? false },
                { "width", def?.WidthInCells ?? 0 },
                { "height", def?.HeightInCells ?? 0 },
                { "build_location_rule", def == null ? null : def.BuildLocationRule.ToString() },
                { "orientation", Orientation.Neutral.ToString() },
                { "facade_id", "DEFAULT_FACADE" },
                { "material_categories", categories },
                { "material_tags", materialTags },
                { "required_mass_kg", required },
                { "available_mass_kg", available },
                { "materials_available", materialsAvailable },
                { "candidate_cells", candidateCells }
            };
        }

        private static Dictionary<string, object> ReadBlueprintIdentity(Dictionary<string, object> cell)
        {
            Dictionary<string, object> identity = new Dictionary<string, object>
            {
                { "instance_id", cell["blueprint_instance_id"] },
                // A multi-cell blueprint may be observed through any tracked
                // footprint cell; bind the identity to its canonical root.
                { "cell", cell.ContainsKey("blueprint_root_cell")
                    ? cell["blueprint_root_cell"]
                    : cell["cell"] },
                { "world_id", cell["world_id"] },
                { "prefab_id", cell["blueprint_prefab_id"] },
                { "orientation", cell["blueprint_orientation"] },
                { "material_tags", cell["blueprint_material_tags"] },
                { "in_progress", cell["blueprint_in_progress"] },
                { "is_utility", cell["blueprint_is_utility"] },
                { "fingerprint", cell["fingerprint"] }
            };
            if (cell.ContainsKey("blueprint_progress_percent"))
            {
                identity["progress_percent"] = cell["blueprint_progress_percent"];
            }
            if (cell.ContainsKey("blueprint_worker_instance_id"))
            {
                identity["worker_instance_id"] = cell["blueprint_worker_instance_id"];
            }
            if (cell.ContainsKey("blueprint_root_cell"))
            {
                identity["root_cell"] = cell["blueprint_root_cell"];
            }
            if (cell.ContainsKey("blueprint_footprint_cells"))
            {
                identity["footprint_cells"] = cell["blueprint_footprint_cells"];
            }
            if (cell.ContainsKey("blueprint_material_lineage"))
            {
                identity["material_lineage"] = cell["blueprint_material_lineage"];
            }
            if (cell.ContainsKey("blueprint_priority_class"))
            {
                identity["priority_class"] = cell["blueprint_priority_class"];
            }
            if (cell.ContainsKey("blueprint_priority_value"))
            {
                identity["priority_value"] = cell["blueprint_priority_value"];
            }
            return identity;
        }

        private static Dictionary<string, object> ReadUtilityPathState()
        {
            Dictionary<string, object> paths = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, UtilityPathRecord> pair in UtilityPaths)
            {
                UtilityPathRecord path = pair.Value;
                List<object> cells = new List<object>();
                List<object> instanceIds = new List<object>();
                List<object> activeInstanceIds = new List<object>();
                foreach (BlueprintTargetRecord target in path.Targets)
                {
                    RefreshBlueprintTargetIdentity(target);
                    cells.Add(target.Cell);
                    instanceIds.Add(target.InstanceId);
                    if (target.InstanceId > 0 &&
                        TryFindBlueprint(target.Cell, target.InstanceId, out _, out _, out _))
                    {
                        activeInstanceIds.Add(target.InstanceId);
                    }
                }
                paths[UtilityPathKey(pair.Key)] = new Dictionary<string, object>
                {
                    { "path_id", path.PathId },
                    { "building_def_id", path.BuildingDefId },
                    { "cells", cells },
                    { "blueprint_instance_ids", instanceIds },
                    { "active_blueprint_instance_ids", activeInstanceIds },
                    { "active", activeInstanceIds.Count > 0 },
                    { "fully_active", activeInstanceIds.Count == instanceIds.Count && instanceIds.Count > 0 }
                };
            }
            return paths;
        }

        private static Dictionary<string, object> ReadBlueprintCell(
            int cell,
            int worldId,
            Dictionary<string, List<Tag>> selectedMaterialsByDef)
        {
            GameObject blueprint;
            Constructable constructable;
            Building building;
            FindAnyBlueprint(cell, out blueprint, out constructable, out building);
            KPrefabID prefab = blueprint?.GetComponent<KPrefabID>();
            int instanceId = prefab == null ? 0 : prefab.InstanceID;
            string prefabId = building?.Def?.PrefabID ?? "none";
            string orientation = building == null ? "none" : building.Orientation.ToString();
            object workerObject = null;
            float rawProgress = -1f;
            if (constructable != null)
            {
                // These getters are live state. A getter failure must fail closed
                // for the whole blueprint read rather than publish a false value.
                workerObject = constructable.GetWorker();
                rawProgress = constructable.GetPercentComplete();
            }
            bool inProgress = constructable != null &&
                (workerObject != null || rawProgress > 0f);
            int progressPercent = -1;
            if (constructable != null &&
                !float.IsNaN(rawProgress) && !float.IsInfinity(rawProgress) &&
                rawProgress >= 0f && rawProgress <= 1f)
            {
                progressPercent = (int)Math.Round(rawProgress * 100f, MidpointRounding.AwayFromZero);
            }
            int workerInstanceId = 0;
            bool workerInstanceKnown = constructable != null;
            if (workerObject != null)
            {
                Component workerComponent = workerObject as Component;
                KPrefabID workerPrefab = workerComponent == null
                    ? null
                    : workerComponent.GetComponent<KPrefabID>();
                if (workerPrefab == null || workerPrefab.InstanceID <= 0)
                {
                    workerInstanceKnown = false;
                }
                else
                {
                    workerInstanceId = workerPrefab.InstanceID;
                }
            }
            List<object> blueprintMaterials = new List<object>();
            if (constructable?.SelectedElementsTags != null)
            {
                foreach (Tag tag in constructable.SelectedElementsTags)
                {
                    blueprintMaterials.Add(tag.Name);
                }
            }

            int objectCount = CountCellObjects(cell);
            bool empty = IsEmptyBlueprintCell(cell, worldId);
            Dictionary<string, bool> canPlaceByDef = new Dictionary<string, bool>(StringComparer.Ordinal);
            foreach (string allowlistedId in BlueprintAllowlist)
            {
                List<Tag> selection = selectedMaterialsByDef.TryGetValue(allowlistedId, out List<Tag> chosen)
                    ? chosen
                    : new List<Tag>();
                canPlaceByDef[allowlistedId] = IsSafeBlueprintPlacement(
                    cell,
                    worldId,
                    Assets.GetBuildingDef(allowlistedId),
                    selection);
            }
            Element element = Grid.Element[cell];
            string elementId = element?.tag.Name ?? "unknown";
            float mass = Grid.Mass[cell];
            float damage = Grid.Damage[cell];
            string fingerprint = BlueprintCellFingerprint(
                cell,
                worldId,
                elementId,
                mass,
                damage,
                objectCount,
                instanceId,
                prefabId,
                orientation,
                blueprintMaterials,
                inProgress);
            string emptyFingerprint = BlueprintCellFingerprint(
                cell,
                worldId,
                elementId,
                mass,
                damage,
                0,
                0,
                "none",
                "none",
                new List<object>(),
                false);
            Vector2I xy = Grid.CellToXY(cell);
            Dictionary<string, object> result = new Dictionary<string, object>
            {
                { "cell", cell },
                { "x", xy.x },
                { "y", xy.y },
                { "world_id", worldId },
                { "visible", Grid.IsVisible(cell) },
                { "solid", Grid.Solid[cell] },
                { "foundation", Grid.Foundation[cell] },
                { "element_id", elementId },
                { "mass_kg", mass },
                { "damage", damage },
                { "object_count", objectCount },
                { "empty_for_blueprint", empty },
                { "blueprint_present", constructable != null },
                { "blueprint_instance_id", instanceId },
                { "blueprint_prefab_id", prefabId },
                { "blueprint_orientation", orientation },
                { "blueprint_material_tags", blueprintMaterials },
                { "blueprint_in_progress", inProgress },
                { "blueprint_is_utility", building?.Def?.isUtility ?? false },
                { "empty_fingerprint", emptyFingerprint },
                { "fingerprint", fingerprint }
            };
            foreach (KeyValuePair<string, bool> pair in canPlaceByDef)
            {
                result[BlueprintCanPlaceField(pair.Key)] = pair.Value;
            }
            if (progressPercent >= 0)
            {
                result["blueprint_progress_percent"] = progressPercent;
            }
            if (workerInstanceKnown)
            {
                result["blueprint_worker_instance_id"] = workerInstanceId;
            }
            if (TryReadBlueprintFootprint(building, worldId, out int rootCell, out List<object> footprintCells))
            {
                result["blueprint_root_cell"] = rootCell;
                result["blueprint_footprint_cells"] = footprintCells;
            }
            if (constructable != null &&
                TryReadBlueprintMaterialLineage(
                    constructable,
                    worldId,
                    instanceId,
                    out List<object> materialLineage))
            {
                result["blueprint_material_lineage"] = materialLineage;
            }
            if (constructable != null &&
                TryReadBlueprintPriority(constructable, out string priorityClass, out int priorityValue))
            {
                result["blueprint_priority_class"] = priorityClass;
                result["blueprint_priority_value"] = priorityValue;
            }
            return result;
        }

        private static bool TryReadBlueprintPriority(
            Constructable constructable,
            out string priorityClass,
            out int priorityValue)
        {
            priorityClass = null;
            priorityValue = 0;
            if (constructable == null)
            {
                return false;
            }
            try
            {
                Prioritizable prioritizable = constructable.GetComponent<Prioritizable>();
                if (prioritizable == null)
                {
                    return false;
                }
                PrioritySetting setting = prioritizable.GetMasterPriority();
                int classValue = (int)setting.priority_class;
                if (classValue < -1 || classValue > 4 || setting.priority_value < 1 || setting.priority_value > 9)
                {
                    return false;
                }
                priorityClass = setting.priority_class.ToString();
                priorityValue = setting.priority_value;
                return !string.IsNullOrEmpty(priorityClass);
            }
            catch
            {
                // A missing or throwing native getter is unknown, never a default priority.
                priorityClass = null;
                priorityValue = 0;
                return false;
            }
        }

        private static bool TryReadBlueprintMaterialLineage(
            Constructable constructable,
            int worldId,
            int expectedInstanceId,
            out List<object> lineage)
        {
            lineage = new List<object>();
            if (constructable == null || worldId < 0 || ReadActiveWorldId() != worldId)
            {
                return false;
            }
            try
            {
                GameObject sourceObject = constructable.gameObject;
                KPrefabID sourcePrefab = sourceObject?.GetComponent<KPrefabID>();
                if (sourceObject == null || sourcePrefab == null ||
                    sourceObject.GetMyWorldId() != worldId ||
                    sourcePrefab.InstanceID <= 0 || sourcePrefab.InstanceID != expectedInstanceId)
                {
                    return false;
                }
                Recipe recipe = constructable.Recipe;
                IList<Recipe.Ingredient> ingredients = recipe == null
                    ? null
                    : recipe.GetAllIngredients(constructable.SelectedElementsTags);
                if (ingredients == null || ingredients.Count == 0 || ingredients.Count > 8)
                {
                    return false;
                }
                Storage storage = constructable.GetComponent<Storage>();
                HashSet<string> seenTags = new HashSet<string>(StringComparer.Ordinal);
                foreach (Recipe.Ingredient ingredient in ingredients)
                {
                    string ingredientTag = ingredient.tag.Name;
                    if (!ingredient.tag.IsValid ||
                        string.IsNullOrEmpty(ingredientTag) || ingredientTag.Length > 64 ||
                        float.IsNaN(ingredient.amount) || float.IsInfinity(ingredient.amount) ||
                        ingredient.amount < 0f ||
                        !seenTags.Add(ingredientTag))
                    {
                        return false;
                    }
                    Dictionary<string, object> item = new Dictionary<string, object>
                    {
                        { "tag", ingredientTag },
                        { "required_mass_kg", ingredient.amount },
                        { "delivered_mass_kg", null },
                        { "remaining_mass_kg", null },
                        { "delivery_status", "unknown" }
                    };
                    if (storage != null)
                    {
                        float delivered = storage.GetMassAvailable(ingredient.tag);
                        if (float.IsNaN(delivered) || float.IsInfinity(delivered) || delivered < 0f)
                        {
                            return false;
                        }
                        if (delivered > ingredient.amount + 0.0001f)
                        {
                            return false;
                        }
                        float remaining = ingredient.amount - delivered;
                        if (remaining < 0f)
                        {
                            remaining = 0f;
                        }
                        item["delivered_mass_kg"] = delivered;
                        item["remaining_mass_kg"] = remaining;
                        item["delivery_status"] = delivered + 0.0001f >= ingredient.amount
                            ? "complete"
                            : "pending";
                    }
                    lineage.Add(item);
                }
                KPrefabID currentPrefab = sourceObject.GetComponent<KPrefabID>();
                return ReadActiveWorldId() == worldId &&
                    sourceObject.GetMyWorldId() == worldId &&
                    sourceObject.GetComponent<Constructable>() == constructable &&
                    currentPrefab != null && currentPrefab.InstanceID == expectedInstanceId;
            }
            catch
            {
                lineage = new List<object>();
                return false;
            }
        }

        private static bool TryReadBlueprintFootprint(
            Building building,
            int worldId,
            out int rootCell,
            out List<object> footprintCells)
        {
            rootCell = 0;
            footprintCells = new List<object>();
            if (building == null || worldId < 0 || ReadActiveWorldId() != worldId)
            {
                return false;
            }
            try
            {
                int candidateRoot = building.GetBottomLeftCell();
                IEnumerable<int> placementCells = building.PlacementCells;
                if (!Grid.IsValidCellInWorld(candidateRoot, worldId) || placementCells == null)
                {
                    return false;
                }
                List<int> ordered = new List<int>();
                foreach (int placementCell in placementCells)
                {
                    if (!Grid.IsValidCellInWorld(placementCell, worldId) ||
                        ordered.Contains(placementCell))
                    {
                        return false;
                    }
                    ordered.Add(placementCell);
                    if (ordered.Count > 64)
                    {
                        return false;
                    }
                }
                if (ordered.Count == 0)
                {
                    return false;
                }
                ordered.Sort();
                if (!ordered.Contains(candidateRoot))
                {
                    return false;
                }
                foreach (int placementCell in ordered)
                {
                    footprintCells.Add(placementCell);
                }
                if (ReadActiveWorldId() != worldId)
                {
                    rootCell = 0;
                    footprintCells = new List<object>();
                    return false;
                }
                rootCell = candidateRoot;
                return true;
            }
            catch
            {
                rootCell = 0;
                footprintCells = new List<object>();
                return false;
            }
        }

        private static void RefreshBlueprintCandidates(int worldId)
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            if (cachedBlueprintCandidates != null &&
                cachedBlueprintWorldId == worldId &&
                now < nextBlueprintCandidateRefreshAt)
            {
                return;
            }
            WorldContainer world = ClusterManager.Instance.GetWorld(worldId);
            if (world == null)
            {
                cachedBlueprintCandidates = new Dictionary<string, List<int>>(StringComparer.Ordinal);
                foreach (string defId in BlueprintAllowlist)
                {
                    cachedBlueprintCandidates[defId] = new List<int>();
                }
                cachedBlueprintWorldId = worldId;
                nextBlueprintCandidateRefreshAt = now.AddSeconds(BlueprintCandidateRefreshSeconds);
                return;
            }
            Dictionary<string, List<Tag>> selectedByDef = new Dictionary<string, List<Tag>>(StringComparer.Ordinal);
            foreach (string defId in BlueprintAllowlist)
            {
                BuildingDef def = Assets.GetBuildingDef(defId);
                List<Tag> selected;
                List<float> ignored;
                bool ignoredAvailable;
                if (TrySelectBlueprintMaterials(def, world, out selected, out ignored, out ignoredAvailable))
                {
                    selectedByDef[defId] = selected;
                }
            }
            int minX;
            int minY;
            int maxX;
            int maxY;
            GetActiveWorldBounds(world, out minX, out minY, out maxX, out maxY);
            int width = maxX - minX + 1;
            int height = maxY - minY + 1;
            int totalCells = Math.Max(0, width * height);
            bool sameWorld = cachedBlueprintCandidates != null && cachedBlueprintWorldId == worldId;
            if (!sameWorld)
            {
                cachedBlueprintCandidates = new Dictionary<string, List<int>>(StringComparer.Ordinal);
                foreach (string defId in BlueprintAllowlist)
                {
                    cachedBlueprintCandidates[defId] = new List<int>();
                }
                cachedBlueprintWorldId = worldId;
                int centerX = (minX + maxX) / 2;
                int centerY = (minY + maxY) / 2;
                blueprintCandidateScanOffset = Math.Max(0, (centerY - minY) * width + centerX - minX);
            }
            else
            {
                foreach (string defId in BlueprintAllowlist)
                {
                    List<int> candidates = cachedBlueprintCandidates[defId];
                    if (!selectedByDef.TryGetValue(defId, out List<Tag> selected))
                    {
                        candidates.Clear();
                        continue;
                    }
                    BuildingDef def = Assets.GetBuildingDef(defId);
                    candidates.RemoveAll(cell => !IsSafeBlueprintPlacement(cell, worldId, def, selected));
                }
            }

            Stopwatch budget = Stopwatch.StartNew();
            int checkedCells = 0;
            while (totalCells > 0 &&
                   checkedCells < CandidateScanCellBudget &&
                   !BlueprintCandidateListsFull(selectedByDef) &&
                   budget.Elapsed.TotalMilliseconds < BlueprintCandidateScanBudgetMs)
            {
                int offset = blueprintCandidateScanOffset % totalCells;
                blueprintCandidateScanOffset = (blueprintCandidateScanOffset + 1) % totalCells;
                checkedCells++;
                int x = minX + offset % width;
                int y = minY + offset / width;
                int cell = Grid.XYToCell(x, y);
                if (!IsEmptyBlueprintCell(cell, worldId))
                {
                    continue;
                }
                foreach (KeyValuePair<string, List<Tag>> pair in selectedByDef)
                {
                    List<int> candidates = cachedBlueprintCandidates[pair.Key];
                    if (candidates.Count >= BlueprintCandidateLimit || candidates.Contains(cell))
                    {
                        continue;
                    }
                    BuildingDef def = Assets.GetBuildingDef(pair.Key);
                    if (IsSafeBlueprintPlacement(cell, worldId, def, pair.Value))
                    {
                        candidates.Add(cell);
                    }
                }
            }
            nextBlueprintCandidateRefreshAt = now.AddSeconds(
                BlueprintCandidateListsFull(selectedByDef) ? BlueprintCandidateRefreshSeconds : 1);
        }

        private static bool BlueprintCandidateListsFull(Dictionary<string, List<Tag>> selectedByDef)
        {
            if (selectedByDef.Count == 0)
            {
                return true;
            }
            foreach (string defId in selectedByDef.Keys)
            {
                if (cachedBlueprintCandidates[defId].Count < BlueprintCandidateLimit)
                {
                    return false;
                }
            }
            return true;
        }

        private static void GetActiveWorldBounds(
            WorldContainer world,
            out int minX,
            out int minY,
            out int maxX,
            out int maxY)
        {
            Vector2I min;
            Vector2I max;
            try
            {
                Grid.GetVisibleCellRangeInActiveWorld(out min, out max, padding: 0, rangeScale: 1f);
            }
            catch
            {
                min = world.WorldOffset;
                max = new Vector2I(
                    world.WorldOffset.x + world.Width - 1,
                    world.WorldOffset.y + world.Height - 1);
            }
            minX = Math.Max(min.x, world.WorldOffset.x);
            minY = Math.Max(min.y, world.WorldOffset.y);
            maxX = Math.Min(max.x, world.WorldOffset.x + world.Width - 1);
            maxY = Math.Min(max.y, world.WorldOffset.y + world.Height - 1);
        }

        internal static bool IsSafeBlueprintPlacement(
            int cell,
            int worldId,
            BuildingDef def,
            IList<Tag> selectedMaterials)
        {
            if (def == null ||
                Array.IndexOf(BlueprintAllowlist, def.PrefabID) < 0 ||
                !Grid.IsValidCellInWorld(cell, worldId) ||
                !IsEmptyBlueprintCell(cell, worldId) ||
                Game.Instance == null ||
                Game.Instance.SandboxModeActive ||
                DebugHandler.InstantBuildMode)
            {
                return false;
            }
            WorldContainer world = ClusterManager.Instance?.GetWorld(worldId);
            if (world == null ||
                !IsBlueprintUnlocked(def) ||
                !ValidateBlueprintMaterials(def, selectedMaterials))
            {
                return false;
            }
            // Every footprint cell (a 1x2 oxygen building also covers the
            // cell above its root) must be as empty as the root; the game's
            // placement rules below then decide floor support and obstruction.
            CellOffset[] offsets = def.PlacementOffsets;
            if (offsets != null)
            {
                foreach (CellOffset offset in offsets)
                {
                    int footprintCell = Grid.OffsetCell(cell, offset);
                    if (!Grid.IsValidCellInWorld(footprintCell, worldId) ||
                        !IsEmptyBlueprintCell(footprintCell, worldId))
                    {
                        return false;
                    }
                }
            }
            try
            {
                return def.IsValidPlaceLocation(
                        null,
                        cell,
                        Orientation.Neutral,
                        replace_tile: false,
                        out _,
                        restrictToActiveWorld: true) &&
                    def.IsValidBuildLocation(
                        null,
                        cell,
                        Orientation.Neutral,
                        replace_tile: false,
                        out _);
            }
            catch
            {
                return false;
            }
        }

        internal static string BlueprintCanPlaceField(string defId)
        {
            StringBuilder builder = new StringBuilder("can_place_");
            for (int index = 0; index < defId.Length; index++)
            {
                char current = defId[index];
                if (char.IsUpper(current))
                {
                    if (index > 0)
                    {
                        builder.Append('_');
                    }
                    builder.Append(char.ToLowerInvariant(current));
                }
                else
                {
                    builder.Append(current);
                }
            }
            return builder.ToString();
        }

        // The build menu hides a building until its technology is researched
        // (TechItems.IsTechItemComplete is true for buildings no tech unlocks);
        // placing one through code respects the same lock.
        private static bool IsBlueprintUnlocked(BuildingDef def)
        {
            try
            {
                return def != null &&
                    def.IsAvailable() &&
                    Db.Get()?.TechItems?.IsTechItemComplete(def.PrefabID) == true;
            }
            catch
            {
                return false;
            }
        }

        internal static bool ValidateBlueprintMaterials(BuildingDef def, IList<Tag> selectedMaterials)
        {
            if (def?.CraftRecipe?.Ingredients == null ||
                selectedMaterials == null ||
                selectedMaterials.Count != def.CraftRecipe.Ingredients.Count)
            {
                return false;
            }
            for (int index = 0; index < selectedMaterials.Count; index++)
            {
                List<Tag> valid = MaterialSelector.GetValidMaterials(
                    def.CraftRecipe.Ingredients[index].tag,
                    omitDisabledElements: true);
                if (!valid.Contains(selectedMaterials[index]))
                {
                    return false;
                }
            }
            return true;
        }

        private static bool TrySelectBlueprintMaterials(
            BuildingDef def,
            WorldContainer world,
            out List<Tag> selected,
            out List<float> availableMasses,
            out bool materialsAvailable)
        {
            selected = new List<Tag>();
            availableMasses = new List<float>();
            materialsAvailable = false;
            if (def?.CraftRecipe?.Ingredients == null || world == null)
            {
                return false;
            }
            foreach (Recipe.Ingredient ingredient in def.CraftRecipe.Ingredients)
            {
                List<Tag> valid = MaterialSelector.GetValidMaterials(ingredient.tag, omitDisabledElements: true);
                Tag best = default(Tag);
                float bestAmount = -1f;
                foreach (Tag candidate in valid)
                {
                    if (DiscoveredResources.Instance != null && !DiscoveredResources.Instance.IsDiscovered(candidate))
                    {
                        continue;
                    }
                    float amount = world.worldInventory.GetAmount(candidate, includeRelatedWorlds: true);
                    if (amount > bestAmount ||
                        (Math.Abs(amount - bestAmount) < 0.0001f &&
                         string.Compare(candidate.Name, best.Name, StringComparison.Ordinal) < 0))
                    {
                        best = candidate;
                        bestAmount = amount;
                    }
                }
                if (!best.IsValid)
                {
                    return false;
                }
                selected.Add(best);
                availableMasses.Add(bestAmount);
            }
            materialsAvailable = ValidateBlueprintMaterials(def, selected) && def.MaterialsAvailable(selected, world);
            return true;
        }

        private static bool IsEmptyBlueprintCell(int cell, int worldId)
        {
            if (!Grid.IsValidCellInWorld(cell, worldId) ||
                !Grid.IsVisible(cell) ||
                Grid.Solid[cell] ||
                Grid.Foundation[cell] ||
                Diggable.GetDiggable(cell) != null)
            {
                return false;
            }
            Element element = Grid.Element[cell];
            if (element == null || element.IsLiquid || Grid.Mass[cell] > 5f)
            {
                return false;
            }
            return CountCellObjects(cell) == 0;
        }

        private static int CountCellObjects(int cell)
        {
            HashSet<GameObject> objects = new HashSet<GameObject>();
            for (int layer = 0; layer < 45; layer++)
            {
                GameObject gameObject = Grid.Objects[cell, layer];
                if (gameObject != null)
                {
                    objects.Add(gameObject);
                }
            }
            return objects.Count;
        }

        private static void FindAnyBlueprint(
            int cell,
            out GameObject blueprint,
            out Constructable constructable,
            out Building building)
        {
            blueprint = null;
            constructable = null;
            building = null;
            for (int layer = 0; layer < 45; layer++)
            {
                GameObject gameObject = Grid.Objects[cell, layer];
                if (gameObject == null)
                {
                    continue;
                }
                Constructable candidate = gameObject.GetComponent<Constructable>();
                if (candidate == null || gameObject.GetComponent<BuildingComplete>() != null)
                {
                    continue;
                }
                blueprint = gameObject;
                constructable = candidate;
                building = gameObject.GetComponent<Building>();
                return;
            }
        }

        internal static bool TryFindBlueprint(
            int cell,
            int instanceId,
            out GameObject blueprint,
            out Constructable constructable,
            out Building building)
        {
            blueprint = null;
            constructable = null;
            building = null;
            if (instanceId <= 0 || !Grid.IsValidCell(cell))
            {
                return false;
            }
            HashSet<GameObject> visited = new HashSet<GameObject>();
            for (int layer = 0; layer < 45; layer++)
            {
                GameObject gameObject = Grid.Objects[cell, layer];
                if (gameObject == null || !visited.Add(gameObject))
                {
                    continue;
                }
                KPrefabID prefab = gameObject.GetComponent<KPrefabID>();
                Constructable candidate = gameObject.GetComponent<Constructable>();
                if (prefab == null ||
                    prefab.InstanceID != instanceId ||
                    candidate == null ||
                    gameObject.GetComponent<BuildingComplete>() != null)
                {
                    continue;
                }
                blueprint = gameObject;
                constructable = candidate;
                building = gameObject.GetComponent<Building>();
                return building != null;
            }
            return false;
        }

        internal static bool TryFindBlueprintAtCell(
            int cell,
            out GameObject blueprint,
            out Constructable constructable,
            out Building building,
            out int instanceId)
        {
            FindAnyBlueprint(cell, out blueprint, out constructable, out building);
            KPrefabID prefab = blueprint == null ? null : blueprint.GetComponent<KPrefabID>();
            instanceId = prefab == null ? 0 : prefab.InstanceID;
            return blueprint != null && constructable != null && building != null && instanceId > 0;
        }

        private static void RefreshBlueprintTargetIdentity(BlueprintTargetRecord target)
        {
            if (target == null || target.Blueprint == null)
            {
                if (target != null)
                {
                    target.Blueprint = null;
                }
                return;
            }
            KPrefabID prefab = target.Blueprint.GetComponent<KPrefabID>();
            if (prefab != null && prefab.InstanceID > 0)
            {
                target.InstanceId = prefab.InstanceID;
            }
        }

        private static string BlueprintCellFingerprint(
            int cell,
            int worldId,
            string elementId,
            float mass,
            float damage,
            int objectCount,
            int blueprintInstanceId,
            string blueprintPrefabId,
            string orientation,
            List<object> materialTags,
            bool inProgress)
        {
            List<string> materialNames = new List<string>();
            foreach (object material in materialTags)
            {
                materialNames.Add(material?.ToString() ?? string.Empty);
            }
            string canonical = string.Join(
                "|",
                cell.ToString(CultureInfo.InvariantCulture),
                worldId.ToString(CultureInfo.InvariantCulture),
                elementId ?? "unknown",
                mass.ToString("R", CultureInfo.InvariantCulture),
                damage.ToString("R", CultureInfo.InvariantCulture),
                objectCount.ToString(CultureInfo.InvariantCulture),
                blueprintInstanceId.ToString(CultureInfo.InvariantCulture),
                blueprintPrefabId ?? "none",
                orientation ?? "none",
                string.Join(",", materialNames.ToArray()),
                inProgress ? "1" : "0");
            using (SHA256 sha256 = SHA256.Create())
            {
                byte[] digest = sha256.ComputeHash(Encoding.UTF8.GetBytes(canonical));
                return "sha256:" + BitConverter.ToString(digest).Replace("-", string.Empty).ToLowerInvariant();
            }
        }

        internal static string UtilityPathKey(string pathId)
        {
            return "id_" + pathId;
        }

        private static Dictionary<string, object> ReadResources()
        {
            string foodStatus;
            string foodSource;
            string foodCompleteness;
            object foodKcal = ReadFoodKcal(out foodStatus, out foodSource, out foodCompleteness);
            return new Dictionary<string, object>
            {
                { "food_kcal", foodKcal },
                { "oxygen_kg_estimate", null },
                { "water_kg_estimate", null },
                { "_food_status", foodStatus },
                { "_food_source", foodSource },
                { "_food_completeness", foodCompleteness }
            };
        }

        private static object ReadFoodKcal(
            out string status,
            out string source,
            out string completeness)
        {
            const string inventorySource =
                "ClusterManager.activeWorld.worldInventory.GetPickupables(GameTags.Edible)+!GameTags.StoredPrivate";
            object inventoryCalories = SumInventoryFoodCalories(out bool bounded, out bool failed);
            source = inventorySource;
            if (failed)
            {
                status = "error";
                completeness = "unknown";
            }
            else if (inventoryCalories == null)
            {
                status = "unsupported";
                completeness = "unknown";
            }
            else
            {
                status = bounded ? "estimated" : "observed";
                completeness = bounded ? "bounded" : "complete";
            }
            return inventoryCalories;
        }

        private static object SumInventoryFoodCalories(out bool bounded, out bool failed)
        {
            bounded = false;
            failed = false;
            try
            {
                WorldInventory inventory = ClusterManager.Instance?.activeWorld?.worldInventory;
                if (inventory == null)
                {
                    return null;
                }

                ICollection<Pickupable> pickupables = inventory.GetPickupables(GameTags.Edible);
                if (pickupables == null)
                {
                    return null;
                }

                bounded = pickupables.Count > EnumerationLimit;
                double total = 0.0;
                int enumerated = 0;
                foreach (Pickupable pickupable in pickupables)
                {
                    if (enumerated >= EnumerationLimit)
                    {
                        bounded = true;
                        break;
                    }
                    enumerated++;

                    if (pickupable == null)
                    {
                        continue;
                    }

                    if (pickupable.KPrefabID.HasTag(GameTags.StoredPrivate))
                    {
                        continue;
                    }

                    Edible edible = pickupable.GetComponent<Edible>();
                    if (edible != null &&
                        (float.IsNaN(edible.Calories) || float.IsInfinity(edible.Calories)))
                    {
                        failed = true;
                        return null;
                    }
                    if (edible != null && edible.Calories > 0.0f)
                    {
                        // Edible.Calories uses ONI's internal calorie unit.
                        // The UI and this protocol expose kcal.
                        total += edible.Calories / 1000.0;
                    }
                }

                return Math.Round(total, 1);
            }
            catch
            {
                failed = true;
                return null;
            }
        }

        private static Dictionary<string, object> ReadResearch()
        {
            object current = null;
            string status = "unsupported";
            const string source = "Research.GetActiveResearch";
            try
            {
                if (Research.Instance != null)
                {
                    TechInstance active = Research.Instance.GetActiveResearch();
                    current = active == null || active.tech == null ? "" : active.tech.Id;
                    status = "observed";
                }
            }
            catch
            {
                status = "error";
            }

            // 2026-09-29: the model loop's research ladder skips finished
            // technologies when this set is observed (before, a finished tech
            // was offered and refused by the executor).  Ordinal-sorted,
            // duplicate-free tech ids, bounded; null with "unsupported" when
            // the research database is not available.
            object complete = null;
            string completeStatus = "unsupported";
            try
            {
                List<Tech> techs = Db.Get()?.Techs?.resources;
                if (Research.Instance != null && techs != null)
                {
                    HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
                    List<string> finished = new List<string>();
                    foreach (Tech tech in techs)
                    {
                        string id = tech?.Id;
                        if (string.IsNullOrEmpty(id) || id.Length > TechIdLengthLimit || !seen.Add(id))
                        {
                            continue;
                        }

                        TechInstance instance = Research.Instance.Get(tech);
                        if (instance != null && instance.IsComplete())
                        {
                            finished.Add(id);
                        }
                    }

                    if (finished.Count <= CompletedTechLimit)
                    {
                        finished.Sort(StringComparer.Ordinal);
                        List<object> items = new List<object>(finished.Count);
                        foreach (string id in finished)
                        {
                            items.Add(id);
                        }

                        complete = items;
                        completeStatus = "observed";
                    }
                }
            }
            catch
            {
                completeStatus = "error";
            }

            return new Dictionary<string, object>
            {
                { "current", current },
                { "complete", complete },
                { "_current_status", status },
                { "_current_source", source },
                { "_complete_status", completeStatus },
                { "_complete_source", CommandFieldContracts.ResearchCompleteSource }
            };
        }

        private static Dictionary<string, object> ReadBuildings()
        {
            int activeWorldId = ReadActiveWorldId();
            DateTimeOffset currentTime = DateTimeOffset.UtcNow;
            if (cachedBuildings != null &&
                cachedBuildingsWorldId == activeWorldId &&
                currentTime < nextBuildingsRefreshAt)
            {
                return cachedBuildings;
            }

            List<object> sampleIds = new List<object>();
            Dictionary<string, int> prefabCounts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            Dictionary<string, object> counts = new Dictionary<string, object>
            {
                { "oxygen_diffuser", 0 },
                { "outhouse", 0 },
                { "manual_generator", 0 },
                { "_scanned", 0 },
                { "_active_world_id", activeWorldId },
                { "_source", "unavailable" },
                { "_sample_ids", sampleIds },
                { "_top_prefabs", new List<object>() }
            };

            int scanned = 0;
            IEnumerable<BuildingComplete> typedItems = ReadActiveWorldBuildingCompletes(activeWorldId);
            if (typedItems != null)
            {
                counts["_source"] = "typed_active_world";
                foreach (BuildingComplete item in typedItems)
                {
                    string id = item?.Def?.PrefabID;
                    if (string.IsNullOrEmpty(id))
                    {
                        continue;
                    }

                    AddBuildingDiagnostic(counts, sampleIds, prefabCounts, id);
                    scanned++;
                    if (scanned >= EnumerationLimit)
                    {
                        break;
                    }
                }
            }
            else
            {
                IEnumerable items = GetComponentRegistryItems("BuildingCompletes");
                if (items == null)
                {
                    CacheBuildings(counts, activeWorldId, currentTime, 5);
                    return counts;
                }

                counts["_source"] = "reflection_all_worlds";
                foreach (object item in items)
                {
                    if (item == null)
                    {
                        continue;
                    }

                    string id = ReadPrefabId(item);
                    if (string.IsNullOrEmpty(id))
                    {
                        continue;
                    }

                    AddBuildingDiagnostic(counts, sampleIds, prefabCounts, id);
                    scanned++;
                    if (scanned >= EnumerationLimit)
                    {
                        break;
                    }
                }
            }

            counts["_scanned"] = scanned;
            counts["_top_prefabs"] = BuildTopPrefabs(prefabCounts);
            CacheBuildings(counts, activeWorldId, currentTime, BuildingRefreshSeconds);
            return counts;
        }

        private static void AddBuildingDiagnostic(
            Dictionary<string, object> counts,
            List<object> sampleIds,
            Dictionary<string, int> prefabCounts,
            string id)
        {
            AddSampleId(sampleIds, id);
            IncrementPrefab(prefabCounts, id);
            IncrementIfExactMatch(counts, id, "oxygen_diffuser", "OxygenDiffuser");
            IncrementIfExactMatch(counts, id, "oxygen_diffuser", "MineralDeoxidizer");
            IncrementIfMatch(counts, id, "outhouse", "Outhouse");
            IncrementIfMatch(counts, id, "manual_generator", "ManualGenerator");
        }

        private static void CacheBuildings(
            Dictionary<string, object> counts,
            int activeWorldId,
            DateTimeOffset capturedAt,
            int refreshSeconds)
        {
            cachedBuildings = counts;
            cachedBuildingsWorldId = activeWorldId;
            nextBuildingsRefreshAt = capturedAt.AddSeconds(refreshSeconds);
        }

        private static int ReadActiveWorldId()
        {
            try
            {
                return ClusterManager.Instance == null ? -1 : ClusterManager.Instance.activeWorldId;
            }
            catch
            {
                return -1;
            }
        }

        private static IEnumerable<BuildingComplete> ReadActiveWorldBuildingCompletes(int activeWorldId)
        {
            try
            {
                if (activeWorldId < 0)
                {
                    return null;
                }

                return Components.BuildingCompletes.GetWorldItems(activeWorldId);
            }
            catch
            {
                return null;
            }
        }

        private static void IncrementIfMatch(Dictionary<string, object> counts, string id, string key, string prefabId)
        {
            if (id.IndexOf(prefabId, StringComparison.OrdinalIgnoreCase) < 0)
            {
                return;
            }

            counts[key] = Convert.ToInt32(counts[key], CultureInfo.InvariantCulture) + 1;
        }

        private static void IncrementIfExactMatch(Dictionary<string, object> counts, string id, string key, string prefabId)
        {
            if (!string.Equals(id, prefabId, StringComparison.Ordinal))
            {
                return;
            }

            counts[key] = Convert.ToInt32(counts[key], CultureInfo.InvariantCulture) + 1;
        }

        private static void AddSampleId(List<object> sampleIds, string id)
        {
            if (sampleIds.Count >= BuildingSampleLimit)
            {
                return;
            }

            foreach (object sample in sampleIds)
            {
                if (string.Equals(sample as string, id, StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }
            }

            sampleIds.Add(id);
        }

        private static void IncrementPrefab(Dictionary<string, int> prefabCounts, string id)
        {
            if (prefabCounts.TryGetValue(id, out int count))
            {
                prefabCounts[id] = count + 1;
            }
            else
            {
                prefabCounts[id] = 1;
            }
        }

        private static List<object> BuildTopPrefabs(Dictionary<string, int> prefabCounts)
        {
            List<KeyValuePair<string, int>> pairs = new List<KeyValuePair<string, int>>(prefabCounts);
            pairs.Sort((left, right) =>
            {
                int countCompare = right.Value.CompareTo(left.Value);
                return countCompare != 0 ? countCompare : string.Compare(left.Key, right.Key, StringComparison.Ordinal);
            });

            List<object> top = new List<object>();
            int limit = Math.Min(TopPrefabLimit, pairs.Count);
            for (int i = 0; i < limit; i++)
            {
                top.Add(new Dictionary<string, object>
                {
                    { "id", pairs[i].Key },
                    { "count", pairs[i].Value }
                });
            }

            return top;
        }

        private static string ReadPrefabId(object item)
        {
            object def = GetMemberValue(item, "Def");
            object defPrefabId = GetMemberValue(def, "PrefabID");
            if (defPrefabId != null)
            {
                return defPrefabId.ToString();
            }

            object prefabComponent = GetMemberValue(item, "prefabid");
            object prefabTag = GetMemberValue(prefabComponent, "PrefabTag", "PrefabID");
            if (prefabTag != null)
            {
                return prefabTag.ToString();
            }

            object prefabId = GetMemberValue(item, "PrefabID");
            if (prefabId != null)
            {
                return prefabId.ToString();
            }

            object gameObject = GetMemberValue(item, "gameObject");
            object goPrefabId = GetMemberValue(gameObject, "PrefabID");
            if (goPrefabId != null)
            {
                return goPrefabId.ToString();
            }

            return item.GetType().Name;
        }

        private static int? CountComponentRegistry(params string[] registryNames)
        {
            IEnumerable items = GetComponentRegistryItems(registryNames);
            if (items == null)
            {
                return null;
            }

            if (items is ICollection collection)
            {
                return collection.Count;
            }

            object count = GetMemberValue(items, "Count", "get_Count");
            if (count != null)
            {
                return Convert.ToInt32(count, CultureInfo.InvariantCulture);
            }

            int scanned = 0;
            foreach (object ignored in items)
            {
                scanned++;
                if (scanned >= EnumerationLimit)
                {
                    break;
                }
            }

            return scanned;
        }

        private static IEnumerable GetComponentRegistryItems(params string[] registryNames)
        {
            Type componentsType = AccessTools.TypeByName("Components");
            if (componentsType == null)
            {
                return null;
            }

            foreach (string registryName in registryNames)
            {
                object registry = GetStaticMemberValue(componentsType, registryName);
                object items = GetMemberValue(registry, "Items", "items");
                if (items is IEnumerable enumerable)
                {
                    return enumerable;
                }

                if (registry is IEnumerable registryEnumerable)
                {
                    return registryEnumerable;
                }
            }

            return null;
        }

        private static object GetStaticSingleton(string typeName)
        {
            Type type = AccessTools.TypeByName(typeName);
            if (type == null)
            {
                return null;
            }

            return GetStaticMemberValue(type, "Instance", "instance", "s_Instance");
        }

        private static object GetStaticMemberValue(Type type, params string[] names)
        {
            if (type == null)
            {
                return null;
            }

            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;
            foreach (string name in names)
            {
                FieldInfo field = type.GetField(name, flags);
                if (field != null)
                {
                    return field.GetValue(null);
                }

                PropertyInfo property = type.GetProperty(name, flags);
                if (property != null && property.GetIndexParameters().Length == 0)
                {
                    return property.GetValue(null, null);
                }

                MethodInfo method = type.GetMethod(name, flags, null, Type.EmptyTypes, null);
                if (method != null)
                {
                    return method.Invoke(null, null);
                }
            }

            return null;
        }

        private static object GetMemberValue(object target, params string[] names)
        {
            if (target == null)
            {
                return null;
            }

            Type type = target.GetType();
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            foreach (string name in names)
            {
                FieldInfo field = type.GetField(name, flags);
                if (field != null)
                {
                    return field.GetValue(target);
                }

                PropertyInfo property = type.GetProperty(name, flags);
                if (property != null && property.GetIndexParameters().Length == 0)
                {
                    return property.GetValue(target, null);
                }

                MethodInfo method = type.GetMethod(name, flags, null, Type.EmptyTypes, null);
                if (method != null)
                {
                    return method.Invoke(target, null);
                }
            }

            return null;
        }

        private static Dictionary<string, object> BuildQuality(
            Dictionary<string, object> game,
            object cycle,
            Dictionary<string, object> duplicants,
            Dictionary<string, object> resources,
            CanonicalAlertReadResult alertResult,
            Dictionary<string, object> research,
            Dictionary<string, object> buildings,
            Dictionary<string, object> functionalBuildings,
            Dictionary<string, object> functionalBuildingGeometry,
            Dictionary<string, object> functionalBuildingPorts,
            Dictionary<string, object> functionalBuildingOccupancy,
            Dictionary<string, object> functionalBuildingHealth,
            Dictionary<string, object> functionalBuildingFlooded,
            Dictionary<string, object> functionalBuildingReachability,
            Dictionary<string, object> functionalBuildingPowerNetwork,
            Dictionary<string, object> functionalBuildingUtilityNetwork,
            Dictionary<string, object> functionalBuildingLogicPorts,
            Dictionary<string, object> functionalBuildingUtilityEndpoint,
            Dictionary<string, object> functionalBuildingUtilityTopology,
            Dictionary<string, object> functionalBuildingUtilityContents,
            Dictionary<string, object> designations,
            BlueprintReadResult blueprintState)
        {
            string pausedStatus = ReadMetadata(game, "_paused_status", "unsupported");
            string pausedSource = ReadMetadata(game, "_paused_source", "SpeedControlScreen.IsPaused");
            string buildModeStatus = ReadMetadata(game, "_build_mode_status", "unsupported");
            const string buildModeSource = "Game.SandboxModeActive+DebugHandler.InstantBuildMode";
            string foodStatus = ReadMetadata(resources, "_food_status", "unsupported");
            string foodSource = ReadMetadata(resources, "_food_source", "unknown");
            string foodCompleteness = ReadMetadata(resources, "_food_completeness", "unknown");
            string researchStatus = ReadMetadata(research, "_current_status", "unsupported");
            string researchSource = ReadMetadata(research, "_current_source", "Research.GetActiveResearch");
            string researchCompleteStatus = ReadMetadata(research, "_complete_status", "unsupported");
            string researchCompleteSource = ReadMetadata(
                research,
                "_complete_source",
                CommandFieldContracts.ResearchCompleteSource);
            string duplicantTotalStatus = ReadMetadata(duplicants, "_total_status", "unsupported");
            string duplicantTotalSource = ReadMetadata(
                duplicants,
                "_total_source",
                "Components.LiveMinionIdentities.active_world");
            string duplicantDetailsStatus = ReadMetadata(duplicants, "_details_status", "unsupported");
            string duplicantDetailsSource = ReadMetadata(
                duplicants,
                "_details_source",
                "Components.LiveMinionIdentities+ChoreConsumer");
            string duplicantIdleStatus = ReadMetadata(duplicants, "_idle_status", "unsupported");
            string duplicantIdleSource = ReadMetadata(
                duplicants,
                "_idle_source",
                CommandFieldContracts.DuplicantIdleSource);
            string duplicantStressedStatus = ReadMetadata(duplicants, "_stressed_status", "unsupported");
            string duplicantStressedSource = ReadMetadata(
                duplicants,
                "_stressed_source",
                CommandFieldContracts.DuplicantStressedSource);
            string buildingSource = ReadMetadata(buildings, "_source", "unavailable");
            string buildingStatus = buildingSource == "unavailable" ? "unsupported" : "estimated";
            string functionalBuildingStatus = ReadMetadata(functionalBuildings, "_status", "unsupported");
            string functionalBuildingSource = ReadMetadata(
                functionalBuildings,
                "_source",
                FunctionalBuildingTelemetryReader.Source);
            string functionalBuildingCompleteness =
                functionalBuildingStatus == "observed" &&
                functionalBuildings != null &&
                functionalBuildings.TryGetValue("complete", out object functionalComplete) &&
                functionalComplete is bool && (bool)functionalComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingQuality = Quality(
                functionalBuildingStatus,
                functionalBuildingSource,
                "completed_building_operational_instances",
                "instance_records",
                "active_world",
                functionalBuildingCompleteness,
                "oni.qc.buildings.functional_instances.v1");
            functionalBuildingQuality["captured_at"] = "functional_buildings.captured_at";
            string functionalBuildingGeometryStatus = ReadMetadata(
                functionalBuildingGeometry,
                "_status",
                "unsupported");
            string functionalBuildingGeometrySource = ReadMetadata(
                functionalBuildingGeometry,
                "_source",
                FunctionalBuildingGeometryTelemetryReader.Source);
            string functionalBuildingGeometryCompleteness =
                functionalBuildingGeometryStatus == "observed" &&
                functionalBuildingGeometry != null &&
                functionalBuildingGeometry.TryGetValue("complete", out object functionalGeometryComplete) &&
                functionalGeometryComplete is bool && (bool)functionalGeometryComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingGeometryQuality = Quality(
                functionalBuildingGeometryStatus,
                functionalBuildingGeometrySource,
                "completed_building_geometry_instances",
                "instance_records",
                "active_world",
                functionalBuildingGeometryCompleteness,
                "oni.qc.buildings.functional_geometry_instances.v1");
            functionalBuildingGeometryQuality["captured_at"] = "functional_building_geometry.captured_at";
            string functionalBuildingPortsStatus = ReadMetadata(
                functionalBuildingPorts,
                "_status",
                "unsupported");
            string functionalBuildingPortsSource = ReadMetadata(
                functionalBuildingPorts,
                "_source",
                FunctionalBuildingPortsTelemetryReader.Source);
            string functionalBuildingPortsCompleteness =
                functionalBuildingPortsStatus == "observed" &&
                functionalBuildingPorts != null &&
                functionalBuildingPorts.TryGetValue("complete", out object functionalPortsComplete) &&
                functionalPortsComplete is bool && (bool)functionalPortsComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingPortsQuality = Quality(
                functionalBuildingPortsStatus,
                functionalBuildingPortsSource,
                "completed_building_declared_ports",
                "port_records",
                "active_world",
                functionalBuildingPortsCompleteness,
                "oni.qc.buildings.functional_ports.v1");
            functionalBuildingPortsQuality["captured_at"] = "functional_building_ports.captured_at";
            string functionalBuildingOccupancyStatus = ReadMetadata(
                functionalBuildingOccupancy,
                "_status",
                "unsupported");
            string functionalBuildingOccupancySource = ReadMetadata(
                functionalBuildingOccupancy,
                "_source",
                FunctionalBuildingOccupancyTelemetryReader.Source);
            string functionalBuildingOccupancyCompleteness =
                functionalBuildingOccupancyStatus == "observed" &&
                functionalBuildingOccupancy != null &&
                functionalBuildingOccupancy.TryGetValue("complete", out object functionalOccupancyComplete) &&
                functionalOccupancyComplete is bool && (bool)functionalOccupancyComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingOccupancyQuality = Quality(
                functionalBuildingOccupancyStatus,
                functionalBuildingOccupancySource,
                "completed_building_occupancy_instances",
                "instance_records",
                "active_world",
                functionalBuildingOccupancyCompleteness,
                "oni.qc.buildings.functional_occupancy_instances.v1");
            functionalBuildingOccupancyQuality["captured_at"] = "functional_building_occupancy.captured_at";
            string functionalBuildingHealthStatus = ReadMetadata(
                functionalBuildingHealth,
                "_status",
                "unsupported");
            string functionalBuildingHealthSource = ReadMetadata(
                functionalBuildingHealth,
                "_source",
                FunctionalBuildingHealthTelemetryReader.Source);
            string functionalBuildingHealthCompleteness =
                functionalBuildingHealthStatus == "observed" &&
                functionalBuildingHealth != null &&
                functionalBuildingHealth.TryGetValue("complete", out object functionalHealthComplete) &&
                functionalHealthComplete is bool && (bool)functionalHealthComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingHealthQuality = Quality(
                functionalBuildingHealthStatus,
                functionalBuildingHealthSource,
                "completed_building_health_instances",
                "instance_records",
                "active_world",
                functionalBuildingHealthCompleteness,
                "oni.qc.buildings.functional_health_instances.v1");
            functionalBuildingHealthQuality["captured_at"] = "functional_building_health.captured_at";
            string functionalBuildingFloodedStatus = ReadMetadata(
                functionalBuildingFlooded,
                "_status",
                "unsupported");
            string functionalBuildingFloodedSource = ReadMetadata(
                functionalBuildingFlooded,
                "_source",
                FunctionalBuildingFloodedTelemetryReader.Source);
            string functionalBuildingFloodedCompleteness =
                functionalBuildingFloodedStatus == "observed" &&
                functionalBuildingFlooded != null &&
                functionalBuildingFlooded.TryGetValue("complete", out object functionalFloodedComplete) &&
                functionalFloodedComplete is bool && (bool)functionalFloodedComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingFloodedQuality = Quality(
                functionalBuildingFloodedStatus,
                functionalBuildingFloodedSource,
                "completed_building_flooded_instances",
                "instance_records",
                "active_world",
                functionalBuildingFloodedCompleteness,
                "oni.qc.buildings.functional_flooded_instances.v1");
            functionalBuildingFloodedQuality["captured_at"] = "functional_building_flooded.captured_at";
            string functionalBuildingReachabilityStatus = ReadMetadata(
                functionalBuildingReachability, "_status", "unsupported");
            string functionalBuildingReachabilitySource = ReadMetadata(
                functionalBuildingReachability, "_source",
                FunctionalBuildingReachabilityTelemetryReader.Source);
            string functionalBuildingReachabilityCompleteness =
                functionalBuildingReachabilityStatus == "observed" &&
                functionalBuildingReachability != null &&
                functionalBuildingReachability.TryGetValue("complete", out object functionalReachabilityComplete) &&
                functionalReachabilityComplete is bool && (bool)functionalReachabilityComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingReachabilityQuality = Quality(
                functionalBuildingReachabilityStatus,
                functionalBuildingReachabilitySource,
                "completed_building_reachability_instances",
                "instance_records",
                "active_world",
                functionalBuildingReachabilityCompleteness,
                "oni.qc.buildings.functional_reachability_instances.v1");
            functionalBuildingReachabilityQuality["captured_at"] =
                "functional_building_reachability.captured_at";
            string functionalBuildingPowerNetworkStatus = ReadMetadata(
                functionalBuildingPowerNetwork, "_status", "unsupported");
            string functionalBuildingPowerNetworkSource = ReadMetadata(
                functionalBuildingPowerNetwork, "_source",
                FunctionalBuildingPowerNetworkTelemetryReader.Source);
            string functionalBuildingPowerNetworkCompleteness =
                functionalBuildingPowerNetworkStatus == "observed" &&
                functionalBuildingPowerNetwork != null &&
                functionalBuildingPowerNetwork.TryGetValue("complete", out object functionalPowerNetworkComplete) &&
                functionalPowerNetworkComplete is bool && (bool)functionalPowerNetworkComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingPowerNetworkQuality = Quality(
                functionalBuildingPowerNetworkStatus,
                functionalBuildingPowerNetworkSource,
                "completed_building_power_network_instances",
                "instance_records",
                "active_world",
                functionalBuildingPowerNetworkCompleteness,
                "oni.qc.buildings.functional_power_network_instances.v1");
            functionalBuildingPowerNetworkQuality["captured_at"] =
                "functional_building_power_network.captured_at";
            string functionalBuildingUtilityNetworkStatus = ReadMetadata(
                functionalBuildingUtilityNetwork, "_status", "unsupported");
            string functionalBuildingUtilityNetworkSource = ReadMetadata(
                functionalBuildingUtilityNetwork, "_source",
                FunctionalBuildingUtilityNetworkTelemetryReader.Source);
            string functionalBuildingUtilityNetworkCompleteness =
                functionalBuildingUtilityNetworkStatus == "observed" &&
                functionalBuildingUtilityNetwork != null &&
                functionalBuildingUtilityNetwork.TryGetValue("complete", out object functionalUtilityNetworkComplete) &&
                functionalUtilityNetworkComplete is bool && (bool)functionalUtilityNetworkComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingUtilityNetworkQuality = Quality(
                functionalBuildingUtilityNetworkStatus,
                functionalBuildingUtilityNetworkSource,
                "completed_building_utility_network_instances",
                "instance_records",
                "active_world",
                functionalBuildingUtilityNetworkCompleteness,
                "oni.qc.buildings.functional_utility_network_instances.v1");
            functionalBuildingUtilityNetworkQuality["captured_at"] =
                "functional_building_utility_network.captured_at";
            string functionalBuildingLogicPortsStatus = ReadMetadata(
                functionalBuildingLogicPorts, "_status", "unsupported");
            string functionalBuildingLogicPortsSource = ReadMetadata(
                functionalBuildingLogicPorts, "_source",
                FunctionalBuildingLogicPortsTelemetryReader.Source);
            string functionalBuildingLogicPortsCompleteness =
                functionalBuildingLogicPortsStatus == "observed" &&
                functionalBuildingLogicPorts != null &&
                functionalBuildingLogicPorts.TryGetValue("complete", out object functionalLogicPortsComplete) &&
                functionalLogicPortsComplete is bool && (bool)functionalLogicPortsComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingLogicPortsQuality = Quality(
                functionalBuildingLogicPortsStatus,
                functionalBuildingLogicPortsSource,
                "completed_building_logic_ports_instances",
                "instance_records",
                "active_world",
                functionalBuildingLogicPortsCompleteness,
                "oni.qc.buildings.functional_logic_ports_instances.v1");
            functionalBuildingLogicPortsQuality["captured_at"] =
                "functional_building_logic_ports.captured_at";
            string functionalBuildingUtilityEndpointStatus = ReadMetadata(
                functionalBuildingUtilityEndpoint, "_status", "unsupported");
            string functionalBuildingUtilityEndpointSource = ReadMetadata(
                functionalBuildingUtilityEndpoint, "_source",
                FunctionalBuildingUtilityEndpointTelemetryReader.Source);
            string functionalBuildingUtilityEndpointCompleteness =
                functionalBuildingUtilityEndpointStatus == "observed" &&
                functionalBuildingUtilityEndpoint != null &&
                functionalBuildingUtilityEndpoint.TryGetValue("complete", out object functionalEndpointComplete) &&
                functionalEndpointComplete is bool && (bool)functionalEndpointComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingUtilityEndpointQuality = Quality(
                functionalBuildingUtilityEndpointStatus,
                functionalBuildingUtilityEndpointSource,
                "completed_building_utility_endpoint_instances",
                "instance_records",
                "active_world",
                functionalBuildingUtilityEndpointCompleteness,
                "oni.qc.buildings.functional_utility_endpoint_instances.v1");
            functionalBuildingUtilityEndpointQuality["captured_at"] =
                "functional_building_utility_endpoint.captured_at";
            string functionalBuildingUtilityTopologyStatus = ReadMetadata(
                functionalBuildingUtilityTopology, "_status", "unsupported");
            string functionalBuildingUtilityTopologySource = ReadMetadata(
                functionalBuildingUtilityTopology, "_source",
                FunctionalBuildingUtilityTopologyTelemetryReader.Source);
            string functionalBuildingUtilityTopologyCompleteness =
                functionalBuildingUtilityTopologyStatus == "observed" &&
                functionalBuildingUtilityTopology != null &&
                functionalBuildingUtilityTopology.TryGetValue("complete", out object functionalTopologyComplete) &&
                functionalTopologyComplete is bool && (bool)functionalTopologyComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingUtilityTopologyQuality = Quality(
                functionalBuildingUtilityTopologyStatus,
                functionalBuildingUtilityTopologySource,
                "completed_building_utility_topology_instances",
                "instance_records",
                "active_world",
                functionalBuildingUtilityTopologyCompleteness,
                "oni.qc.buildings.functional_utility_topology_instances.v1");
            functionalBuildingUtilityTopologyQuality["captured_at"] =
                "functional_building_utility_topology.captured_at";
            string functionalBuildingUtilityContentsStatus = ReadMetadata(
                functionalBuildingUtilityContents, "_status", "unsupported");
            string functionalBuildingUtilityContentsSource = ReadMetadata(
                functionalBuildingUtilityContents, "_source",
                FunctionalBuildingUtilityContentsTelemetryReader.Source);
            string functionalBuildingUtilityContentsCompleteness =
                functionalBuildingUtilityContentsStatus == "observed" &&
                functionalBuildingUtilityContents != null &&
                functionalBuildingUtilityContents.TryGetValue("complete", out object functionalContentsComplete) &&
                functionalContentsComplete is bool && (bool)functionalContentsComplete
                    ? "complete"
                    : "unknown";
            Dictionary<string, object> functionalBuildingUtilityContentsQuality = Quality(
                functionalBuildingUtilityContentsStatus,
                functionalBuildingUtilityContentsSource,
                "completed_building_utility_contents_instances",
                "instance_records",
                "active_world",
                functionalBuildingUtilityContentsCompleteness,
                "oni.qc.buildings.functional_utility_contents_instances.v1");
            functionalBuildingUtilityContentsQuality["captured_at"] =
                "functional_building_utility_contents.captured_at";
            Dictionary<string, object> dig = designations["dig"] as Dictionary<string, object>;
            string digStatus = ReadMetadata(dig, "_status", "unsupported");
            string digToolStatus = ReadMetadata(dig, "_tool_status", "unsupported");
            string digSource = ReadMetadata(dig, "_source", "Grid+DigTool+Components.Diggables");
            string blueprintStatus = blueprintState?.Status ?? "unsupported";
            string blueprintSource = blueprintState?.Source ?? "BuildingDef+Grid+Constructable+WorldInventory";
            string alertStatus = alertResult?.Status ?? "error";
            string alertCompleteness = alertResult?.Completeness ?? "unknown";
            Dictionary<string, object> alertQuality = Quality(
                alertStatus,
                CanonicalAlertReader.Source,
                "canonical_active_alert_ids",
                "alert_id_list",
                CanonicalAlertReader.Scope,
                alertCompleteness,
                CommandFieldContracts.CanonicalAlertsId);
            Dictionary<string, object> alertWorldQuality = Quality(
                alertStatus,
                CanonicalAlertReader.Source,
                "canonical_active_alert_ids_by_world",
                "world_alert_groups",
                CanonicalAlertReader.Scope,
                alertCompleteness);
            string alertFailureReason = alertResult?.FailureReason ?? "alert_result_missing";
            if (!string.IsNullOrEmpty(alertFailureReason))
            {
                // Diagnostic only: executor authorization remains bound to the
                // trusted contract fields above and never consumes this reason.
                alertQuality["reason"] = alertFailureReason;
                alertWorldQuality["reason"] = alertFailureReason;
            }

            Dictionary<string, object> quality = new Dictionary<string, object>
            {
                { "game.paused", Quality(
                    pausedStatus,
                    pausedSource,
                    "direct_state",
                    "boolean",
                    "active_game_session",
                    pausedStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.GamePausedId) },
                { "game.sandbox_mode", Quality(
                    buildModeStatus,
                    buildModeSource,
                    "direct_state",
                    "boolean",
                    "active_game_session",
                    buildModeStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.GameBuildModeId) },
                { "game.instant_build_mode", Quality(
                    buildModeStatus,
                    buildModeSource,
                    "direct_state",
                    "boolean",
                    "active_game_session",
                    buildModeStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.GameBuildModeId) },
                { "cycle", Quality(cycle == null ? "error" : "observed", "GameClock.GetCycle", "display_1_based") },
                { "duplicants.total", Quality(
                    duplicantTotalStatus,
                    duplicantTotalSource,
                    "live_count",
                    "count",
                    "active_world",
                    duplicantTotalStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.ActiveWorldDuplicantCountId) },
                { "duplicants.stressed", Quality(
                    duplicantStressedStatus,
                    duplicantStressedSource,
                    "stressed_count",
                    "count",
                    "active_world",
                    duplicantStressedStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.DuplicantStressedId) },
                { "duplicants.idle", Quality(
                    duplicantIdleStatus,
                    duplicantIdleSource,
                    "idle_count",
                    "count",
                    "active_world",
                    duplicantIdleStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.DuplicantIdleId) },
                { "duplicants.by_instance_id", Quality(duplicantDetailsStatus, duplicantDetailsSource) },
                { "resources.food_kcal", Quality(
                    foodStatus,
                    foodSource,
                    "registered_non_private_inventory_sum",
                    "kcal",
                    "active_world_registered_non_private_edibles",
                    foodCompleteness,
                    CommandFieldContracts.RegisteredFoodId) },
                { "resources.oxygen_kg_estimate", Quality("unsupported", "none") },
                { "resources.water_kg_estimate", Quality("unsupported", "none") },
                { "alerts", alertQuality },
                { "alerts_by_world", alertWorldQuality },
                { "research.current", Quality(
                    researchStatus,
                    researchSource,
                    "active_selection",
                    "technology_id_or_empty",
                    "save_research",
                    researchStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.ResearchCurrentId) },
                { "research.complete", Quality(
                    researchCompleteStatus,
                    researchCompleteSource,
                    "completed_technology_ids",
                    "technology_id_list",
                    "save_research",
                    researchCompleteStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.ResearchCompleteId) },
                { "buildings.oxygen_diffuser", Quality(buildingStatus, buildingSource) },
                { "buildings.outhouse", Quality(buildingStatus, buildingSource) },
                { "buildings.manual_generator", Quality(buildingStatus, buildingSource) },
                { "buildings._source", Quality(buildingSource == "unavailable" ? "unsupported" : "observed", buildingSource) },
                { "buildings._scanned", Quality(buildingSource == "unavailable" ? "unsupported" : "observed", buildingSource) },
                { "functional_buildings", functionalBuildingQuality },
                { "functional_building_geometry", functionalBuildingGeometryQuality },
                { "functional_building_ports", functionalBuildingPortsQuality },
                { "functional_building_occupancy", functionalBuildingOccupancyQuality },
                { "functional_building_health", functionalBuildingHealthQuality },
                { "functional_building_flooded", functionalBuildingFloodedQuality },
                { "functional_building_reachability", functionalBuildingReachabilityQuality },
                { "functional_building_power_network", functionalBuildingPowerNetworkQuality },
                { "functional_building_utility_network", functionalBuildingUtilityNetworkQuality },
                { "functional_building_logic_ports", functionalBuildingLogicPortsQuality },
                { "functional_building_utility_endpoint", functionalBuildingUtilityEndpointQuality },
                { "functional_building_utility_topology", functionalBuildingUtilityTopologyQuality },
                { "functional_building_utility_contents", functionalBuildingUtilityContentsQuality },
                { "designations.dig.tool.available", Quality("observed", digSource) },
                { "designations.dig.tool.tiles_filter_on", Quality(
                    digToolStatus,
                    digSource,
                    "dig_tool_filter_state",
                    "boolean",
                    "active_game_session",
                    digToolStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.DigToolId) },
                { "designations.dig.tool.natural_backwall_filter_on", Quality(
                    digToolStatus,
                    digSource,
                    "dig_tool_filter_state",
                    "boolean",
                    "active_game_session",
                    digToolStatus == "observed" ? "complete" : "unknown",
                    CommandFieldContracts.DigToolId) },
                { "designations.dig.candidate_cells", Quality(digStatus, digSource) },
                { "designations.dig.by_cell", Quality(digStatus, digSource) },
                { "blueprints.catalog", Quality(blueprintStatus, blueprintSource) },
                { "blueprints.by_instance_id", Quality(blueprintStatus, blueprintSource) },
                { "blueprints.utility_paths", Quality(blueprintStatus, blueprintSource) },
                { "cells.by_id", Quality(blueprintStatus, blueprintSource) }
            };
            AddDuplicantPriorityQuality(quality, duplicants, duplicantDetailsStatus, duplicantDetailsSource);
            AddDigCellQuality(quality, dig, digStatus, digSource);
            AddBlueprintQuality(quality, blueprintState, blueprintStatus, blueprintSource);
            return quality;
        }

        private static void AddBlueprintQuality(
            Dictionary<string, object> quality,
            BlueprintReadResult blueprintState,
            string status,
            string source)
        {
            if (!string.Equals(status, "observed", StringComparison.Ordinal) || blueprintState == null)
            {
                return;
            }

            AddNestedObjectQuality(
                quality,
                blueprintState.Blueprints,
                "catalog",
                "blueprints.catalog.",
                new[]
                {
                    "available",
                    "prefab_id",
                    "is_utility",
                    "cancellable",
                    "orientation",
                    "facade_id",
                    "material_tags"
                },
                status,
                source,
                "blueprint_catalog_field",
                "field_native",
                "approved_blueprint_catalog",
                "complete",
                CommandFieldContracts.BlueprintCatalogId);
            AddNestedObjectQuality(
                quality,
                blueprintState.Blueprints,
                "catalog",
                "blueprints.catalog.",
                new[]
                {
                    "width",
                    "height",
                    "build_location_rule",
                    "material_categories",
                    "required_mass_kg",
                    "available_mass_kg",
                    "materials_available",
                    "candidate_cells"
                },
                status,
                source,
                "blueprint_catalog_diagnostic_field",
                "field_native",
                "approved_blueprint_catalog",
                "complete",
                null);
            AddNestedObjectQuality(
                quality,
                blueprintState.Blueprints,
                "by_instance_id",
                "blueprints.by_instance_id.",
                new[]
                {
                    "instance_id",
                    "cell",
                    "world_id",
                    "prefab_id",
                    "orientation",
                    "material_tags",
                    "in_progress",
                    "is_utility",
                    "fingerprint",
                    "progress_percent",
                    "worker_instance_id",
                    "root_cell",
                    "footprint_cells",
                    "priority_class",
                    "priority_value"
                },
                status,
                source,
                "blueprint_instance_state_field",
                "field_native",
                "active_world_registered_blueprints",
                "complete_for_registered_instance",
                CommandFieldContracts.BlueprintInstanceId);
            AddNestedObjectQuality(
                quality,
                blueprintState.Blueprints,
                "by_instance_id",
                "blueprints.by_instance_id.",
                new[] { "material_lineage" },
                status,
                "Constructable.Recipe+Storage.GetMassAvailable",
                "blueprint_material_lineage",
                "mass_kg_by_recipe_ingredient",
                "active_world_tracked_blueprint",
                "complete_for_constructable",
                "oni.qc.blueprints.material_lineage.v1");
            AddNestedObjectQuality(
                quality,
                blueprintState.Blueprints,
                "utility_paths",
                "blueprints.utility_paths.",
                new[]
                {
                    "path_id",
                    "building_def_id",
                    "cells",
                    "blueprint_instance_ids",
                    "active_blueprint_instance_ids",
                    "active",
                    "fully_active"
                },
                status,
                source,
                "utility_blueprint_path_state_field",
                "field_native",
                "probe_tracked_utility_paths",
                "complete_for_tracked_path",
                CommandFieldContracts.UtilityPathId);
            AddNestedObjectQuality(
                quality,
                blueprintState.Cells,
                "by_id",
                "cells.by_id.",
                new[]
                {
                    "cell",
                    "x",
                    "y",
                    "world_id",
                    "visible",
                    "solid",
                    "foundation",
                    "element_id",
                    "mass_kg",
                    "damage",
                    "object_count",
                    "empty_for_blueprint",
                    "can_place_ladder",
                    "can_place_wire",
                    "blueprint_present",
                    "blueprint_instance_id",
                    "blueprint_prefab_id",
                    "blueprint_orientation",
                    "blueprint_material_tags",
                    "blueprint_in_progress",
                    "blueprint_is_utility",
                    "blueprint_progress_percent",
                    "blueprint_worker_instance_id",
                    "blueprint_root_cell",
                    "blueprint_footprint_cells",
                    "blueprint_priority_class",
                    "blueprint_priority_value",
                    "empty_fingerprint",
                    "fingerprint"
                },
                status,
                source,
                "blueprint_cell_state_field",
                "field_native",
                "active_world_tracked_cells",
                "complete_for_tracked_cell",
                CommandFieldContracts.BlueprintCellId);
            AddNestedObjectQuality(
                quality,
                blueprintState.Cells,
                "by_id",
                "cells.by_id.",
                new[] { "blueprint_material_lineage" },
                status,
                "Constructable.Recipe+Storage.GetMassAvailable",
                "blueprint_material_lineage",
                "mass_kg_by_recipe_ingredient",
                "active_world_tracked_blueprint",
                "complete_for_constructable",
                "oni.qc.blueprints.material_lineage.v1");
        }

        private static void AddNestedObjectQuality(
            Dictionary<string, object> quality,
            Dictionary<string, object> container,
            string collectionKey,
            string prefix,
            IEnumerable<string> fields,
            string status,
            string source,
            string semantic,
            string unit,
            string scope,
            string completeness,
            string contractId)
        {
            if (container == null ||
                !container.TryGetValue(collectionKey, out object collectionValue) ||
                !(collectionValue is Dictionary<string, object> collection))
            {
                return;
            }

            foreach (KeyValuePair<string, object> item in collection)
            {
                if (!(item.Value is Dictionary<string, object> details))
                {
                    continue;
                }
                foreach (string field in fields)
                {
                    if (details.ContainsKey(field))
                    {
                        quality[prefix + item.Key + "." + field] = Quality(
                            status,
                            source,
                            semantic,
                            unit,
                            scope,
                            completeness,
                            contractId);
                    }
                }
            }
        }

        private static void AddDigCellQuality(
            Dictionary<string, object> quality,
            Dictionary<string, object> dig,
            string status,
            string source)
        {
            if (!string.Equals(status, "observed", StringComparison.Ordinal) ||
                dig == null ||
                !(dig["by_cell"] is Dictionary<string, object> byCell))
            {
                return;
            }

            string[] fields =
            {
                "cell",
                "world_id",
                "solid",
                "element_id",
                "mass_kg",
                "damage",
                "designation_present",
                "designation_instance_id",
                "designation_kind",
                "designation_in_progress",
                "safe_mark_candidate",
                "fingerprint"
            };
            foreach (string key in byCell.Keys)
            {
                string prefix = "designations.dig.by_cell." + key + ".";
                foreach (string field in fields)
                {
                    quality[prefix + field] = Quality(
                        "observed",
                        source,
                        "dig_cell_state_field",
                        "field_native",
                        "active_world_tracked_cells",
                        "complete_for_tracked_cell",
                        CommandFieldContracts.DigCellId);
                }
            }
        }

        private static void AddDuplicantPriorityQuality(
            Dictionary<string, object> quality,
            Dictionary<string, object> duplicants,
            string status,
            string source)
        {
            if (!string.Equals(status, "observed", StringComparison.Ordinal) ||
                !(duplicants["by_instance_id"] is Dictionary<string, object> byInstanceId))
            {
                return;
            }

            foreach (KeyValuePair<string, object> duplicant in byInstanceId)
            {
                if (!(duplicant.Value is Dictionary<string, object> details))
                {
                    continue;
                }

                string prefix = "duplicants.by_instance_id." + duplicant.Key;
                quality[prefix + ".instance_id"] = Quality("observed", source);
                quality[prefix + ".name"] = Quality("observed", source);
                quality[prefix + ".world_id"] = Quality("observed", source);
                if (!(details["chore_priorities"] is Dictionary<string, object> priorities))
                {
                    continue;
                }

                foreach (string groupId in priorities.Keys)
                {
                    quality[prefix + ".chore_priorities." + groupId] = Quality(
                        "observed",
                        source,
                        "configured_chore_priority",
                        "priority_0_to_5",
                        "active_world_live_duplicant",
                        "complete",
                        CommandFieldContracts.DuplicantPriorityId);
                }
            }
        }

        private static Dictionary<string, object> Quality(
            string status,
            string source,
            string semantic = null,
            string unit = null,
            string scope = null,
            string completeness = null,
            string contractId = null)
        {
            Dictionary<string, object> value = new Dictionary<string, object>
            {
                { "status", status },
                { "source", source }
            };
            if (!string.IsNullOrEmpty(semantic))
            {
                value["semantic"] = semantic;
            }
            if (!string.IsNullOrEmpty(unit))
            {
                value["unit"] = unit;
            }
            if (!string.IsNullOrEmpty(scope))
            {
                value["scope"] = scope;
            }
            if (!string.IsNullOrEmpty(completeness))
            {
                value["completeness"] = completeness;
            }
            if (!string.IsNullOrEmpty(contractId))
            {
                value["contract_id"] = contractId;
            }
            if (!string.IsNullOrEmpty(unit) || !string.IsNullOrEmpty(scope) || !string.IsNullOrEmpty(completeness))
            {
                value["captured_at"] = "snapshot.ts";
            }

            return value;
        }

        private static string ReadMetadata(Dictionary<string, object> mapping, string key, string fallback)
        {
            object value;
            if (mapping != null && mapping.TryGetValue(key, out value) && value != null)
            {
                return Convert.ToString(value, CultureInfo.InvariantCulture);
            }

            return fallback;
        }

        private static T Timed<T>(string name, Func<T> reader, Dictionary<string, object> metrics)
        {
            Stopwatch stopwatch = Stopwatch.StartNew();
            try
            {
                return reader();
            }
            finally
            {
                stopwatch.Stop();
                metrics[name] = Math.Round(stopwatch.Elapsed.TotalMilliseconds, 3);
            }
        }

        private static object NormalizeNumber(object value)
        {
            if (value == null)
            {
                return null;
            }

            try
            {
                switch (Type.GetTypeCode(value.GetType()))
                {
                    case TypeCode.Byte:
                    case TypeCode.SByte:
                    case TypeCode.Int16:
                    case TypeCode.UInt16:
                    case TypeCode.Int32:
                    case TypeCode.UInt32:
                    case TypeCode.Int64:
                    case TypeCode.UInt64:
                        return Convert.ToInt64(value, CultureInfo.InvariantCulture);
                    case TypeCode.Single:
                    case TypeCode.Double:
                    case TypeCode.Decimal:
                        return Convert.ToDouble(value, CultureInfo.InvariantCulture);
                    default:
                        return value;
                }
            }
            catch
            {
                return value.ToString();
            }
        }

        private sealed class DuplicantCountReadResult
        {
            internal int? Total { get; set; }

            internal int? Idle { get; set; }

            internal int? Stressed { get; set; }

            internal string TotalStatus { get; set; } = "unsupported";

            internal string IdleStatus { get; set; } = "unsupported";

            internal string StressedStatus { get; set; } = "unsupported";

            internal void MarkEnumerationError()
            {
                Total = null;
                Idle = null;
                Stressed = null;
                TotalStatus = "error";
                IdleStatus = "error";
                StressedStatus = "error";
            }
        }

        private sealed class BlueprintReadResult
        {
            internal Dictionary<string, object> Blueprints { get; } =
                new Dictionary<string, object>(StringComparer.Ordinal)
                {
                    { "catalog", new Dictionary<string, object>(StringComparer.Ordinal) },
                    { "by_instance_id", new Dictionary<string, object>(StringComparer.Ordinal) },
                    { "utility_paths", new Dictionary<string, object>(StringComparer.Ordinal) }
                };

            internal Dictionary<string, object> Cells { get; } =
                new Dictionary<string, object>(StringComparer.Ordinal)
                {
                    { "by_id", new Dictionary<string, object>(StringComparer.Ordinal) }
                };

            internal string Status { get; set; } = "unsupported";

            internal string Source { get; set; } =
                "BuildingDef+Grid+Constructable+WorldInventory";
        }

        private sealed class UtilityPathRecord
        {
            internal string PathId { get; set; }

            internal string BuildingDefId { get; set; }

            internal List<BlueprintTargetRecord> Targets { get; } =
                new List<BlueprintTargetRecord>();
        }

        private sealed class BlueprintTargetRecord
        {
            internal int Cell { get; set; }

            internal int InstanceId { get; set; }

            internal GameObject Blueprint { get; set; }
        }

    }
}
