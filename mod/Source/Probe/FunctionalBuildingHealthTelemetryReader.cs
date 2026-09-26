using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads native enabled/health state for every completed building in the
    /// active world.  This is an additive diagnostic sibling to
    /// FunctionalBuildingTelemetryReader: it intentionally does not repeat the
    /// existing operational field.
    /// </summary>
    internal static class FunctionalBuildingHealthTelemetryReader
    {
        internal const string Schema = "oni.functional_building_health.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+BuildingEnabledButton.IsEnabled+BuildingHP.HitPoints/MaxHitPoints/IsBroken/NeedsRepairs";

        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<HealthInstance> observedInstances = new List<HealthInstance>();
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

                    HealthInstance health;
                    if (!TryReadHealth(item, prefab.InstanceID, itemWorldId, out health))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_health_read_error");
                    }
                    boundItems.Add(item);
                    observedInstances.Add(health);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Re-read identity and every optional value.  Any change means
                // this was a mixed-time snapshot, so fail closed.
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

                    HealthInstance current;
                    if (!TryReadHealth(item, prefab.InstanceID, activeWorldId, out current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_health_read_error");
                    }
                    if (!HealthEquals(observedInstances[index], current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_health_changed_during_read");
                    }
                }

                observedInstances.Sort(CompareInstances);
                return Payload(capturedAt, activeWorldId, true, observedInstances, "observed", null);
            }
            catch
            {
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_health_read_error");
            }
        }

        private static bool TryReadHealth(
            BuildingComplete item,
            int instanceId,
            int worldId,
            out HealthInstance health)
        {
            health = null;
            if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 ||
                item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID))
            {
                return false;
            }

            bool? enabled = null;
            BuildingEnabledButton enabledButton = item.GetComponent<BuildingEnabledButton>();
            if (enabledButton != null)
            {
                try { enabled = enabledButton.IsEnabled; } catch { enabled = null; }
            }

            int? hitPoints = null;
            int? maxHitPoints = null;
            bool? broken = null;
            bool? needsRepairs = null;
            BuildingHP buildingHp = item.GetComponent<BuildingHP>();
            if (buildingHp != null)
            {
                try { hitPoints = buildingHp.HitPoints >= 0 ? (int?)buildingHp.HitPoints : null; } catch { hitPoints = null; }
                try { maxHitPoints = buildingHp.MaxHitPoints >= 0 ? (int?)buildingHp.MaxHitPoints : null; } catch { maxHitPoints = null; }
                try { broken = buildingHp.IsBroken; } catch { broken = null; }
                try { needsRepairs = buildingHp.NeedsRepairs; } catch { needsRepairs = null; }
            }

            health = new HealthInstance
            {
                InstanceId = instanceId,
                WorldId = worldId,
                PrefabId = item.Def.PrefabID,
                Enabled = enabled,
                HitPoints = hitPoints,
                MaxHitPoints = maxHitPoints,
                Broken = broken,
                NeedsRepairs = needsRepairs
            };
            return true;
        }

        private static bool HealthEquals(HealthInstance left, HealthInstance right)
        {
            return left != null && right != null &&
                left.InstanceId == right.InstanceId &&
                left.WorldId == right.WorldId &&
                string.Equals(left.PrefabId, right.PrefabId, StringComparison.Ordinal) &&
                left.Enabled == right.Enabled &&
                left.HitPoints == right.HitPoints &&
                left.MaxHitPoints == right.MaxHitPoints &&
                left.Broken == right.Broken &&
                left.NeedsRepairs == right.NeedsRepairs;
        }

        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer activeWorld, int activeWorldId)
        {
            return manager != null && activeWorld != null && activeWorldId >= 0 &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId && activeWorld.id == activeWorldId;
        }

        private static int CompareInstances(HealthInstance left, HealthInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0 ? prefabCompare : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt, int activeWorldId, List<HealthInstance> instances, string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt, int activeWorldId, bool complete,
            List<HealthInstance> instances, string status, string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (HealthInstance instance in instances)
            {
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "enabled", instance.Enabled.HasValue ? (object)instance.Enabled.Value : null },
                    { "hit_points", instance.HitPoints.HasValue ? (object)instance.HitPoints.Value : null },
                    { "max_hit_points", instance.MaxHitPoints.HasValue ? (object)instance.MaxHitPoints.Value : null },
                    { "is_broken", instance.Broken.HasValue ? (object)instance.Broken.Value : null },
                    { "needs_repairs", instance.NeedsRepairs.HasValue ? (object)instance.NeedsRepairs.Value : null }
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

        private sealed class HealthInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal bool? Enabled;
            internal int? HitPoints;
            internal int? MaxHitPoints;
            internal bool? Broken;
            internal bool? NeedsRepairs;
        }
    }
}
