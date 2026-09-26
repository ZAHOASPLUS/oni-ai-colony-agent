using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads placement geometry for completed buildings in the active world.
    /// Every geometry record is bound to the current BuildingComplete registry
    /// and fails closed when the world or placement collection changes while it
    /// is being read.
    /// </summary>
    internal static class FunctionalBuildingGeometryTelemetryReader
    {
        internal const string Schema = "oni.functional_building_geometry.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.Orientation+Building.GetBottomLeftCell+Building.PlacementCells";

        private const int EnumerationLimit = 10000;
        private const int FootprintLimit = 64;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<GeometryInstance> observedInstances = new List<GeometryInstance>();
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

                    Building building = item.GetComponent<Building>();
                    if (building == null)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_component_missing");
                    }

                    GeometryInstance geometry;
                    if (!TryReadGeometry(item, building, prefab.InstanceID, itemWorldId, out geometry, out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }

                    boundItems.Add(item);
                    observedInstances.Add(geometry);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Re-read every bound item.  This catches a registry/world race
                // or a placement mutation after the first observation instead
                // of publishing a mixed-time geometry snapshot.
                for (int index = 0; index < boundItems.Count; index++)
                {
                    BuildingComplete item = boundItems[index];
                    if (item == null || item.gameObject == null)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                    if (item.gameObject.GetMyWorldId() != activeWorldId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    Building building = item.GetComponent<Building>();
                    if (prefab == null || building == null || prefab.InstanceID != observedInstances[index].InstanceId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_identity_changed");
                    }

                    GeometryInstance current;
                    if (!TryReadGeometry(item, building, prefab.InstanceID, activeWorldId, out current, out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }
                    if (!GeometryEquals(observedInstances[index], current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_geometry_changed_during_read");
                    }
                }

                observedInstances.Sort(CompareInstances);
                return Payload(capturedAt, activeWorldId, true, observedInstances, "observed", null);
            }
            catch
            {
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_geometry_read_error");
            }
        }

        private static bool TryReadGeometry(
            BuildingComplete item,
            Building building,
            int instanceId,
            int worldId,
            out GeometryInstance geometry,
            out string reason)
        {
            geometry = null;
            reason = "building_geometry_read_error";
            if (item == null || building == null || worldId < 0 || instanceId <= 0)
            {
                reason = "building_geometry_identity_invalid";
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

                string orientation = building.Orientation.ToString();
                if (string.IsNullOrEmpty(orientation))
                {
                    reason = "building_orientation_invalid";
                    return false;
                }

                int rootCell = building.GetBottomLeftCell();
                if (!Grid.IsValidCellInWorld(rootCell, worldId))
                {
                    reason = "building_root_cell_invalid";
                    return false;
                }

                IEnumerable<int> placementCells = building.PlacementCells;
                if (placementCells == null)
                {
                    reason = "building_footprint_missing";
                    return false;
                }

                List<int> cells = new List<int>();
                HashSet<int> seenCells = new HashSet<int>();
                foreach (int cell in placementCells)
                {
                    if (cells.Count >= FootprintLimit)
                    {
                        reason = "building_footprint_too_many";
                        return false;
                    }
                    if (!Grid.IsValidCellInWorld(cell, worldId))
                    {
                        reason = "building_footprint_cell_invalid";
                        return false;
                    }
                    if (!seenCells.Add(cell))
                    {
                        reason = "building_footprint_duplicate";
                        return false;
                    }
                    cells.Add(cell);
                }

                if (cells.Count == 0)
                {
                    reason = "building_footprint_empty";
                    return false;
                }
                cells.Sort();
                if (!seenCells.Contains(rootCell))
                {
                    reason = "building_root_cell_not_in_footprint";
                    return false;
                }

                geometry = new GeometryInstance
                {
                    InstanceId = instanceId,
                    WorldId = worldId,
                    PrefabId = prefabId,
                    Orientation = orientation,
                    RootCell = rootCell,
                    FootprintCells = cells
                };
                return true;
            }
            catch
            {
                geometry = null;
                reason = "building_geometry_read_error";
                return false;
            }
        }

        private static bool GeometryEquals(GeometryInstance left, GeometryInstance right)
        {
            if (left == null || right == null ||
                left.InstanceId != right.InstanceId ||
                left.WorldId != right.WorldId ||
                !string.Equals(left.PrefabId, right.PrefabId, StringComparison.Ordinal) ||
                !string.Equals(left.Orientation, right.Orientation, StringComparison.Ordinal) ||
                left.RootCell != right.RootCell ||
                left.FootprintCells.Count != right.FootprintCells.Count)
            {
                return false;
            }
            for (int index = 0; index < left.FootprintCells.Count; index++)
            {
                if (left.FootprintCells[index] != right.FootprintCells[index])
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

        private static int CompareInstances(GeometryInstance left, GeometryInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0
                ? prefabCompare
                : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt,
            int activeWorldId,
            List<GeometryInstance> instances,
            string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt,
            int activeWorldId,
            bool complete,
            List<GeometryInstance> instances,
            string status,
            string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (GeometryInstance instance in instances)
            {
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "orientation", instance.Orientation },
                    { "root_cell", instance.RootCell },
                    { "footprint_cells", new List<object>(instance.FootprintCells.ConvertAll(cell => (object)cell)) }
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

        private sealed class GeometryInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal string Orientation;
            internal int RootCell;
            internal List<int> FootprintCells;
        }
    }
}
