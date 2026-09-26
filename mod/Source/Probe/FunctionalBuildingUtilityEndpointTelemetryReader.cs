using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>Reads endpoint registration/ownership only; never rebuilds networks.</summary>
    internal static class FunctionalBuildingUtilityEndpointTelemetryReader
    {
        internal const string Schema = "oni.functional_building_utility_endpoint.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+IUtilityNetworkMgr.GetEndpoint+FlowUtilityNetwork.IItem";
        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset at = DateTimeOffset.UtcNow;
            List<EndpointInstance> observed = new List<EndpointInstance>();
            List<BuildingComplete> bound = new List<BuildingComplete>();
            int worldId = -1;
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                WorldContainer world = manager == null ? null : manager.activeWorld;
                worldId = manager == null ? -1 : manager.activeWorldId;
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_binding_invalid");
                IEnumerable<BuildingComplete> items;
                try { items = Components.BuildingCompletes.GetWorldItems(worldId); }
                catch { return Error(at, worldId, observed, "building_registry_error"); }
                if (items == null) return Error(at, worldId, observed, "building_registry_unavailable");
                HashSet<int> ids = new HashSet<int>();
                int count = 0;
                foreach (BuildingComplete item in items)
                {
                    if (count++ >= EnumerationLimit) return Error(at, worldId, observed, "building_registry_limit");
                    if (item == null || item.gameObject == null) return Error(at, worldId, observed, "building_identity_invalid");
                    int itemWorld;
                    try { itemWorld = item.gameObject.GetMyWorldId(); } catch { return Error(at, worldId, observed, "building_world_read_error"); }
                    if (itemWorld != worldId) return Error(at, worldId, observed, "building_world_mismatch");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !ids.Add(prefab.InstanceID)) return Error(at, worldId, observed, "building_instance_id_invalid");
                    if (item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return Error(at, worldId, observed, "building_prefab_id_invalid");
                    EndpointInstance value;
                    if (!TryRead(item, prefab.InstanceID, itemWorld, out value)) return Error(at, worldId, observed, "building_utility_endpoint_read_error");
                    bound.Add(item); observed.Add(value);
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                for (int i = 0; i < bound.Count; i++)
                {
                    BuildingComplete item = bound[i];
                    if (item == null || item.gameObject == null || item.gameObject.GetMyWorldId() != worldId) return Error(at, worldId, observed, "building_world_binding_changed");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    EndpointInstance current;
                    if (prefab == null || prefab.InstanceID != observed[i].InstanceId || !TryRead(item, prefab.InstanceID, worldId, out current)) return Error(at, worldId, observed, "building_utility_endpoint_read_error");
                    if (!Equals(observed[i], current)) return Error(at, worldId, observed, "building_utility_endpoint_changed_during_read");
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                observed.Sort(Compare);
                return Payload(at, worldId, true, observed, "observed", null);
            }
            catch { return Error(at, worldId, observed, "functional_building_utility_endpoint_read_error"); }
        }

        private static bool TryRead(BuildingComplete item, int instanceId, int worldId, out EndpointInstance value)
        {
            value = null;
            if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 || item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return false;
            value = new EndpointInstance { InstanceId = instanceId, WorldId = worldId, PrefabId = item.Def.PrefabID };
            if (!ReadSide(item, worldId, true, out value.Input) || !ReadSide(item, worldId, false, out value.Output)) { value = null; return false; }
            return true;
        }

        private static bool ReadSide(BuildingComplete item, int worldId, bool input, out EndpointSide side)
        {
            side = new EndpointSide { Declared = input ? item.Def.InputConduitType != ConduitType.None : item.Def.OutputConduitType != ConduitType.None };
            if (!side.Declared) return true;
            try
            {
                ConduitType type = input ? item.Def.InputConduitType : item.Def.OutputConduitType;
                int cell = input ? item.GetUtilityInputCell() : item.GetUtilityOutputCell();
                side.Cell = cell;
                if (!Grid.IsValidCellInWorld(cell, worldId)) return false;
                IUtilityNetworkMgr mgr = type == ConduitType.Solid ? (Game.Instance == null ? null : Game.Instance.solidConduitSystem) : Conduit.GetNetworkManager(type);
                if (mgr == null) { side.Registered = null; return true; }
                object endpoint = mgr.GetEndpoint(cell);
                FlowUtilityNetwork.IItem owned = endpoint as FlowUtilityNetwork.IItem;
                side.Registered = owned != null && ReferenceEquals(owned.GameObject, item.gameObject);
                if (owned != null) { side.EndpointType = owned.EndpointType.ToString(); side.ConduitType = owned.ConduitType.ToString(); }
                return true;
            }
            catch { side.Registered = null; return true; }
        }

        private static bool Equals(EndpointInstance a, EndpointInstance b) { return a != null && b != null && a.InstanceId == b.InstanceId && a.WorldId == b.WorldId && string.Equals(a.PrefabId, b.PrefabId, StringComparison.Ordinal) && Equals(a.Input, b.Input) && Equals(a.Output, b.Output); }
        private static bool Equals(EndpointSide a, EndpointSide b) { return a != null && b != null && a.Declared == b.Declared && a.Cell == b.Cell && a.Registered == b.Registered && a.EndpointType == b.EndpointType && a.ConduitType == b.ConduitType; }
        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer world, int worldId) { return manager != null && world != null && worldId >= 0 && ReferenceEquals(ClusterManager.Instance, manager) && ReferenceEquals(manager.activeWorld, world) && manager.activeWorldId == worldId && world.id == worldId; }
        private static int Compare(EndpointInstance a, EndpointInstance b) { int c = string.CompareOrdinal(a.PrefabId, b.PrefabId); return c != 0 ? c : a.InstanceId.CompareTo(b.InstanceId); }
        private static Dictionary<string, object> Error(DateTimeOffset at, int world, List<EndpointInstance> values, string reason) { return Payload(at, world, false, values, "error", reason); }
        private static Dictionary<string, object> Payload(DateTimeOffset at, int world, bool complete, List<EndpointInstance> values, string status, string reason)
        {
            List<object> records = new List<object>();
            foreach (EndpointInstance v in values) records.Add(new Dictionary<string, object> { { "instance_id", v.InstanceId }, { "world_id", v.WorldId }, { "prefab_id", v.PrefabId }, { "input", Side(v.Input) }, { "output", Side(v.Output) } });
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema", Schema }, { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) }, { "world_id", world }, { "complete", complete }, { "instances", records }, { "_status", status }, { "_source", Source } };
            if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason;
            return result;
        }
        private static Dictionary<string, object> Side(EndpointSide s) { return new Dictionary<string, object> { { "declared", s.Declared }, { "cell", s.Declared ? (object)s.Cell : null }, { "registered", s.Registered.HasValue ? (object)s.Registered.Value : null }, { "endpoint_type", s.EndpointType }, { "conduit_type", s.ConduitType } }; }
        private sealed class EndpointInstance { internal int InstanceId; internal int WorldId; internal string PrefabId; internal EndpointSide Input; internal EndpointSide Output; }
        private sealed class EndpointSide { internal bool Declared; internal int Cell = -1; internal bool? Registered; internal string EndpointType; internal string ConduitType; }
    }
}
