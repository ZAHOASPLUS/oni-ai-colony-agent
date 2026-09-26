using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads occupancy and entombment for completed buildings in the active
    /// world.  Occupancy is sourced only from the native OccupyArea grid-cell
    /// cache; placement cells are deliberately not used as a substitute.
    /// Missing optional components and failed optional getters remain unknown
    /// (null) while a world/identity race fails the snapshot closed.
    /// </summary>
    internal static class FunctionalBuildingOccupancyTelemetryReader
    {
        internal const string Schema = "oni.functional_building_occupancy.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Structure.IsEntombed+OccupyArea.GetOccupiedGridCells";

        private const int EnumerationLimit = 10000;
        private const int OccupiedCellsLimit = 64;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<OccupancyInstance> observedInstances = new List<OccupancyInstance>();
            int activeWorldId = -1;

            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "cluster_manager_unavailable");
                }

                WorldContainer activeWorld = manager.activeWorld;
                activeWorldId = manager.activeWorldId;
                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_binding_invalid");
                }

                IEnumerable<BuildingComplete> items;
                try
                {
                    items = Components.BuildingCompletes.GetWorldItems(activeWorldId);
                }
                catch
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "building_registry_error");
                }

                if (items == null)
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "building_registry_unavailable");
                }

                HashSet<int> instanceIds = new HashSet<int>();
                List<BuildingComplete> boundItems = new List<BuildingComplete>();
                int enumerated = 0;
                foreach (BuildingComplete item in items)
                {
                    if (enumerated >= EnumerationLimit)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_registry_limit");
                    }
                    enumerated++;

                    if (item == null || item.gameObject == null)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_identity_invalid");
                    }

                    int itemWorldId;
                    try
                    {
                        itemWorldId = item.gameObject.GetMyWorldId();
                    }
                    catch
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_read_error");
                    }
                    if (itemWorldId != activeWorldId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_mismatch");
                    }

                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !instanceIds.Add(prefab.InstanceID))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_instance_id_invalid");
                    }
                    if (item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_prefab_id_invalid");
                    }

                    OccupancyInstance occupancy;
                    if (!TryReadOccupancy(
                        item,
                        prefab.InstanceID,
                        itemWorldId,
                        out occupancy,
                        out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }

                    boundItems.Add(item);
                    observedInstances.Add(occupancy);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Re-read every item and optional component used as evidence.
                // A world switch, component mutation, or changed occupancy must
                // not become a falsely complete mixed-time snapshot.
                for (int index = 0; index < boundItems.Count; index++)
                {
                    BuildingComplete item = boundItems[index];
                    if (item == null || item.gameObject == null)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                    int itemWorldId;
                    try
                    {
                        itemWorldId = item.gameObject.GetMyWorldId();
                    }
                    catch
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                    if (itemWorldId != activeWorldId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }

                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID != observedInstances[index].InstanceId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_identity_changed");
                    }

                    OccupancyInstance current;
                    if (!TryReadOccupancy(
                        item,
                        prefab.InstanceID,
                        activeWorldId,
                        out current,
                        out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }
                    if (!OccupancyEquals(observedInstances[index], current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_occupancy_changed_during_read");
                    }
                }

                observedInstances.Sort(CompareInstances);
                return Payload(capturedAt, activeWorldId, true, observedInstances, "observed", null);
            }
            catch
            {
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_occupancy_read_error");
            }
        }

        private static bool TryReadOccupancy(
            BuildingComplete item,
            int instanceId,
            int worldId,
            out OccupancyInstance occupancy,
            out string reason)
        {
            occupancy = null;
            reason = "building_occupancy_read_error";
            if (item == null || item.gameObject == null || worldId < 0 || instanceId <= 0)
            {
                reason = "building_occupancy_identity_invalid";
                return false;
            }

            try
            {
                string prefabId = item.Def?.PrefabID;
                if (string.IsNullOrEmpty(prefabId))
                {
                    reason = "building_prefab_id_invalid";
                    return false;
                }

                // Both components are optional across the completed-building
                // registry.  Their absence is an explicitly unknown value.
                Structure structure = item.GetComponent<Structure>();
                bool? entombed = null;
                if (structure != null)
                {
                    try
                    {
                        entombed = structure.IsEntombed();
                    }
                    catch
                    {
                        entombed = null;
                    }
                }

                OccupyArea occupyArea = item.GetComponent<OccupyArea>();
                List<int> occupiedCells = null;
                if (occupyArea != null)
                {
                    int[] cells = null;
                    try
                    {
                        cells = occupyArea.GetOccupiedGridCells();
                    }
                    catch
                    {
                        cells = null;
                    }

                    if (cells != null)
                    {
                        if (cells.Length > OccupiedCellsLimit)
                        {
                            reason = "building_occupied_cells_too_many";
                            return false;
                        }

                        occupiedCells = new List<int>(cells.Length);
                        HashSet<int> seenCells = new HashSet<int>();
                        foreach (int cell in cells)
                        {
                            if (!Grid.IsValidCellInWorld(cell, worldId))
                            {
                                reason = "building_occupied_cell_invalid";
                                return false;
                            }
                            if (!seenCells.Add(cell))
                            {
                                reason = "building_occupied_cell_duplicate";
                                return false;
                            }
                            occupiedCells.Add(cell);
                        }
                        occupiedCells.Sort();
                    }
                }

                occupancy = new OccupancyInstance
                {
                    InstanceId = instanceId,
                    WorldId = worldId,
                    PrefabId = prefabId,
                    Entombed = entombed,
                    OccupiedCells = occupiedCells
                };
                return true;
            }
            catch
            {
                occupancy = null;
                reason = "building_occupancy_read_error";
                return false;
            }
        }

        private static bool OccupancyEquals(OccupancyInstance left, OccupancyInstance right)
        {
            if (left == null || right == null || left.InstanceId != right.InstanceId ||
                left.WorldId != right.WorldId ||
                !string.Equals(left.PrefabId, right.PrefabId, StringComparison.Ordinal) ||
                left.Entombed != right.Entombed)
            {
                return false;
            }

            if (left.OccupiedCells == null || right.OccupiedCells == null)
            {
                return left.OccupiedCells == null && right.OccupiedCells == null;
            }
            if (left.OccupiedCells.Count != right.OccupiedCells.Count)
            {
                return false;
            }
            for (int index = 0; index < left.OccupiedCells.Count; index++)
            {
                if (left.OccupiedCells[index] != right.OccupiedCells[index])
                {
                    return false;
                }
            }
            return true;
        }

        private static bool WorldBindingMatches(
            ClusterManager manager,
            WorldContainer activeWorld,
            int activeWorldId)
        {
            return manager != null &&
                activeWorld != null &&
                activeWorldId >= 0 &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId &&
                activeWorld.id == activeWorldId;
        }

        private static int CompareInstances(OccupancyInstance left, OccupancyInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0
                ? prefabCompare
                : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt,
            int activeWorldId,
            List<OccupancyInstance> instances,
            string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt,
            int activeWorldId,
            bool complete,
            List<OccupancyInstance> instances,
            string status,
            string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (OccupancyInstance instance in instances)
            {
                object jsonOccupiedCells = null;
                if (instance.OccupiedCells != null)
                {
                    jsonOccupiedCells = new List<object>(
                        instance.OccupiedCells.ConvertAll(cell => (object)cell));
                }
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "entombed", instance.Entombed.HasValue
                        ? (object)instance.Entombed.Value
                        : null },
                    { "occupied_cells", jsonOccupiedCells }
                });
            }

            Dictionary<string, object> result = new Dictionary<string, object>
            {
                { "schema", Schema },
                { "captured_at", capturedAt.ToString("o", CultureInfo.InvariantCulture) },
                { "world_id", activeWorldId },
                { "complete", complete },
                { "instances", jsonInstances },
                { "_status", status },
                { "_source", Source }
            };
            if (!string.IsNullOrEmpty(reason))
            {
                result["_reason"] = reason;
            }
            return result;
        }

        private sealed class OccupancyInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal bool? Entombed;
            internal List<int> OccupiedCells;
        }
    }
}
