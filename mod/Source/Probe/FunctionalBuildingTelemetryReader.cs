using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads the native operational state of a small, explicit set of completed
    /// bootstrap buildings.  This reader is deliberately uncached: an
    /// operational flag is useful only when it is bound to the current
    /// active-world snapshot.
    /// </summary>
    internal static class FunctionalBuildingTelemetryReader
    {
        internal const string Schema = "oni.functional_buildings.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Operational.IsOperational";

        private const int EnumerationLimit = 10000;
        private static readonly string[] SupportedPrefabIds =
        {
            "OxygenDiffuser",
            "MineralDeoxidizer",
            "Outhouse",
            "ManualGenerator"
        };

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<FunctionalBuildingInstance> observedInstances =
                new List<FunctionalBuildingInstance>();
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
                    // If the registry has another item after the bound, the
                    // collection is not complete.  Do not publish a truncated
                    // list as a successful empty/complete observation.
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

                    // Cross-check the component's native prefab tag against
                    // BuildingDef.PrefabID. A disagreement is an identity
                    // failure, never an alias to be guessed by the controller.
                    try
                    {
                        Tag nativePrefabTag = prefab.PrefabID();
                        string nativePrefabId = nativePrefabTag.ToString();
                        if (string.IsNullOrEmpty(nativePrefabId) ||
                            !string.Equals(nativePrefabId, item.Def.PrefabID, StringComparison.Ordinal))
                        {
                            return Error(capturedAt, activeWorldId, observedInstances, "building_prefab_identity_mismatch");
                        }
                    }
                    catch
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_prefab_identity_read_error");
                    }

                    boundItems.Add(item);
                    if (!IsSupportedPrefab(item.Def.PrefabID))
                    {
                        continue;
                    }

                    bool? operationalValue = null;
                    bool operationalReadError = false;
                    try
                    {
                        Operational operational = item.GetComponent<Operational>();
                        if (operational == null)
                        {
                            return Error(capturedAt, activeWorldId, observedInstances, "operational_component_missing");
                        }
                        operationalValue = operational.IsOperational;
                    }
                    catch
                    {
                        // Preserve the instance identity, but never convert a
                        // failed getter into false or a successful collection.
                        operationalReadError = true;
                    }

                    observedInstances.Add(new FunctionalBuildingInstance
                    {
                        InstanceId = prefab.InstanceID,
                        WorldId = itemWorldId,
                        PrefabId = item.Def.PrefabID,
                        Operational = operationalValue
                    });

                    if (operationalReadError)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "operational_read_error");
                    }
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Recheck every registry item that was used as evidence.  A
                // world switch or destruction during enumeration must remain an
                // error rather than becoming a false complete snapshot.
                foreach (BuildingComplete item in boundItems)
                {
                    if (item == null || item.gameObject == null ||
                        item.gameObject.GetMyWorldId() != activeWorldId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                }

                observedInstances.Sort(CompareInstances);
                return Payload(capturedAt, activeWorldId, true, observedInstances, "observed", null);
            }
            catch
            {
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_read_error");
            }
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

        private static bool IsSupportedPrefab(string prefabId)
        {
            for (int index = 0; index < SupportedPrefabIds.Length; index++)
            {
                if (string.Equals(SupportedPrefabIds[index], prefabId, StringComparison.Ordinal))
                {
                    return true;
                }
            }

            return false;
        }

        private static int CompareInstances(
            FunctionalBuildingInstance left,
            FunctionalBuildingInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0
                ? prefabCompare
                : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt,
            int activeWorldId,
            List<FunctionalBuildingInstance> instances,
            string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt,
            int activeWorldId,
            bool complete,
            List<FunctionalBuildingInstance> instances,
            string status,
            string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (FunctionalBuildingInstance instance in instances)
            {
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "operational", instance.Operational.HasValue
                        ? (object)instance.Operational.Value
                        : null }
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

        private sealed class FunctionalBuildingInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal bool? Operational;
        }
    }
}
