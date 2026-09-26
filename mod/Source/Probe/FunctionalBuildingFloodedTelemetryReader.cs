using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads the native Floodable.IsFlooded flag for every completed building
    /// in the active world.  Floodable is optional; a missing component or a
    /// getter failure is represented as an unknown (null) value.  Identity,
    /// world binding, registry and second-read races fail the envelope closed.
    /// </summary>
    internal static class FunctionalBuildingFloodedTelemetryReader
    {
        internal const string Schema = "oni.functional_building_flooded.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Floodable.IsFlooded";

        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<FloodedInstance> observedInstances = new List<FloodedInstance>();
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

                    FloodedInstance flooded;
                    if (!TryReadFlooded(item, prefab.InstanceID, itemWorldId, out flooded))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_flooded_read_error");
                    }
                    boundItems.Add(item);
                    observedInstances.Add(flooded);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Re-read identity and the optional Floodable value.  Any
                // change indicates a mixed-time snapshot and fails closed.
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

                    FloodedInstance current;
                    if (!TryReadFlooded(item, prefab.InstanceID, activeWorldId, out current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_flooded_read_error");
                    }
                    if (!FloodedEquals(observedInstances[index], current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_flooded_changed_during_read");
                    }
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                observedInstances.Sort(CompareInstances);
                return Payload(capturedAt, activeWorldId, true, observedInstances, "observed", null);
            }
            catch
            {
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_flooded_read_error");
            }
        }

        private static bool TryReadFlooded(
            BuildingComplete item,
            int instanceId,
            int worldId,
            out FloodedInstance flooded)
        {
            flooded = null;
            if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 ||
                item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID))
            {
                return false;
            }

            bool? isFlooded = null;
            Floodable floodable = null;
            try { floodable = item.GetComponent<Floodable>(); } catch { floodable = null; }
            if (floodable != null)
            {
                try { isFlooded = floodable.IsFlooded; } catch { isFlooded = null; }
            }

            flooded = new FloodedInstance
            {
                InstanceId = instanceId,
                WorldId = worldId,
                PrefabId = item.Def.PrefabID,
                IsFlooded = isFlooded
            };
            return true;
        }

        private static bool FloodedEquals(FloodedInstance left, FloodedInstance right)
        {
            return left != null && right != null &&
                left.InstanceId == right.InstanceId &&
                left.WorldId == right.WorldId &&
                string.Equals(left.PrefabId, right.PrefabId, StringComparison.Ordinal) &&
                left.IsFlooded == right.IsFlooded;
        }

        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer activeWorld, int activeWorldId)
        {
            return manager != null && activeWorld != null && activeWorldId >= 0 &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId && activeWorld.id == activeWorldId;
        }

        private static int CompareInstances(FloodedInstance left, FloodedInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0 ? prefabCompare : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt, int activeWorldId, List<FloodedInstance> instances, string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt, int activeWorldId, bool complete,
            List<FloodedInstance> instances, string status, string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (FloodedInstance instance in instances)
            {
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "is_flooded", instance.IsFlooded.HasValue ? (object)instance.IsFlooded.Value : null }
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
            if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason;
            return result;
        }

        private sealed class FloodedInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal bool? IsFlooded;
        }
    }
}
