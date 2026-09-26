using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads native Workable/MinionGroupProber reachability for completed
    /// buildings.  This is observation only: it never calls
    /// Workable.RefreshReachability and never infers food, hazard, network, or
    /// operational safety from the result.
    /// </summary>
    internal static class FunctionalBuildingReachabilityTelemetryReader
    {
        internal const string Schema = "oni.functional_building_reachability.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Workable.GetCell/GetOffsets+MinionGroupProber.IsAllReachable";

        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<ReachabilityInstance> observed = new List<ReachabilityInstance>();
            List<BuildingComplete> bound = new List<BuildingComplete>();
            int activeWorldId = -1;
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                    return Error(capturedAt, activeWorldId, observed, "cluster_manager_unavailable");
                WorldContainer activeWorld = manager.activeWorld;
                activeWorldId = manager.activeWorldId;
                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                    return Error(capturedAt, activeWorldId, observed, "active_world_binding_invalid");

                IEnumerable<BuildingComplete> items;
                try { items = Components.BuildingCompletes.GetWorldItems(activeWorldId); }
                catch { return Error(capturedAt, activeWorldId, observed, "building_registry_error"); }
                if (items == null)
                    return Error(capturedAt, activeWorldId, observed, "building_registry_unavailable");

                HashSet<int> ids = new HashSet<int>();
                int count = 0;
                foreach (BuildingComplete item in items)
                {
                    if (count++ >= EnumerationLimit)
                        return Error(capturedAt, activeWorldId, observed, "building_registry_limit");
                    if (item == null || item.gameObject == null)
                        return Error(capturedAt, activeWorldId, observed, "building_identity_invalid");
                    int itemWorldId;
                    try { itemWorldId = item.gameObject.GetMyWorldId(); }
                    catch { return Error(capturedAt, activeWorldId, observed, "building_world_read_error"); }
                    if (itemWorldId != activeWorldId)
                        return Error(capturedAt, activeWorldId, observed, "building_world_mismatch");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !ids.Add(prefab.InstanceID))
                        return Error(capturedAt, activeWorldId, observed, "building_instance_id_invalid");
                    if (item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID))
                        return Error(capturedAt, activeWorldId, observed, "building_prefab_id_invalid");
                    ReachabilityInstance value;
                    if (!TryRead(item, prefab.InstanceID, itemWorldId, out value))
                        return Error(capturedAt, activeWorldId, observed, "building_reachability_read_error");
                    bound.Add(item);
                    observed.Add(value);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                    return Error(capturedAt, activeWorldId, observed, "active_world_changed_during_read");
                for (int i = 0; i < bound.Count; i++)
                {
                    BuildingComplete item = bound[i];
                    if (item == null || item.gameObject == null)
                        return Error(capturedAt, activeWorldId, observed, "building_world_binding_changed");
                    int world;
                    try { world = item.gameObject.GetMyWorldId(); }
                    catch { return Error(capturedAt, activeWorldId, observed, "building_world_binding_changed"); }
                    if (world != activeWorldId)
                        return Error(capturedAt, activeWorldId, observed, "building_world_binding_changed");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID != observed[i].InstanceId)
                        return Error(capturedAt, activeWorldId, observed, "building_identity_changed");
                    ReachabilityInstance current;
                    if (!TryRead(item, prefab.InstanceID, world, out current))
                        return Error(capturedAt, activeWorldId, observed, "building_reachability_read_error");
                    if (!Equals(observed[i], current))
                        return Error(capturedAt, activeWorldId, observed, "building_reachability_changed_during_read");
                }
                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                    return Error(capturedAt, activeWorldId, observed, "active_world_changed_during_read");
                observed.Sort(Compare);
                return Payload(capturedAt, activeWorldId, true, observed, "observed", null);
            }
            catch { return Error(capturedAt, activeWorldId, observed, "functional_building_reachability_read_error"); }
        }

        private static bool TryRead(BuildingComplete item, int instanceId, int worldId, out ReachabilityInstance value)
        {
            value = null;
            if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 ||
                item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return false;
            bool? reachable = null;
            Workable workable = null;
            try { workable = item.GetComponent<Workable>(); } catch { workable = null; }
            if (workable != null)
            {
                try
                {
                    MinionGroupProber prober = MinionGroupProber.Get();
                    CellOffset[] offsets = workable.GetOffsets();
                    if (prober != null && offsets != null)
                        reachable = prober.IsAllReachable(workable.GetCell(), offsets);
                }
                catch { reachable = null; }
            }
            value = new ReachabilityInstance
            {
                InstanceId = instanceId, WorldId = worldId,
                PrefabId = item.Def.PrefabID, IsReachable = reachable
            };
            return true;
        }

        private static bool Equals(ReachabilityInstance a, ReachabilityInstance b)
        {
            return a != null && b != null && a.InstanceId == b.InstanceId && a.WorldId == b.WorldId &&
                string.Equals(a.PrefabId, b.PrefabId, StringComparison.Ordinal) && a.IsReachable == b.IsReachable;
        }

        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer world, int worldId)
        {
            return manager != null && world != null && worldId >= 0 &&
                ReferenceEquals(ClusterManager.Instance, manager) && ReferenceEquals(manager.activeWorld, world) &&
                manager.activeWorldId == worldId && world.id == worldId;
        }

        private static int Compare(ReachabilityInstance a, ReachabilityInstance b)
        {
            int c = string.CompareOrdinal(a.PrefabId, b.PrefabId);
            return c != 0 ? c : a.InstanceId.CompareTo(b.InstanceId);
        }

        private static Dictionary<string, object> Error(DateTimeOffset at, int world, List<ReachabilityInstance> values, string reason)
        { return Payload(at, world, false, values, "error", reason); }

        private static Dictionary<string, object> Payload(DateTimeOffset at, int world, bool complete,
            List<ReachabilityInstance> values, string status, string reason)
        {
            List<object> records = new List<object>();
            foreach (ReachabilityInstance v in values)
                records.Add(new Dictionary<string, object>
                {
                    { "instance_id", v.InstanceId }, { "world_id", v.WorldId },
                    { "prefab_id", v.PrefabId },
                    { "is_reachable", v.IsReachable.HasValue ? (object)v.IsReachable.Value : null }
                });
            Dictionary<string, object> result = new Dictionary<string, object>
            {
                { "schema", Schema }, { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) },
                { "world_id", world }, { "complete", complete }, { "instances", records },
                { "_status", status }, { "_source", Source }
            };
            if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason;
            return result;
        }

        private sealed class ReachabilityInstance
        {
            internal int InstanceId; internal int WorldId; internal string PrefabId; internal bool? IsReachable;
        }
    }
}
