using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>Reads bounded utility-network topology counts only.</summary>
    internal static class FunctionalBuildingUtilityTopologyTelemetryReader
    {
        internal const string Schema = "oni.functional_building_utility_topology.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+IUtilityNetworkMgr.GetNetworkForCell+FlowUtilityNetwork.sources/sinks/conduitCount";
        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset at = DateTimeOffset.UtcNow;
            List<TopologyInstance> observed = new List<TopologyInstance>();
            List<BuildingComplete> bound = new List<BuildingComplete>();
            int worldId = -1;
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                WorldContainer world = manager == null ? null : manager.activeWorld;
                worldId = manager == null ? -1 : manager.activeWorldId;
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_binding_invalid");
                IEnumerable<BuildingComplete> items;
                try { items = Components.BuildingCompletes.GetWorldItems(worldId); } catch { return Error(at, worldId, observed, "building_registry_error"); }
                if (items == null) return Error(at, worldId, observed, "building_registry_unavailable");
                HashSet<int> ids = new HashSet<int>(); int count = 0;
                foreach (BuildingComplete item in items)
                {
                    if (count++ >= EnumerationLimit) return Error(at, worldId, observed, "building_registry_limit");
                    if (item == null || item.gameObject == null) return Error(at, worldId, observed, "building_identity_invalid");
                    int itemWorld; try { itemWorld = item.gameObject.GetMyWorldId(); } catch { return Error(at, worldId, observed, "building_world_read_error"); }
                    if (itemWorld != worldId) return Error(at, worldId, observed, "building_world_mismatch");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !ids.Add(prefab.InstanceID)) return Error(at, worldId, observed, "building_instance_id_invalid");
                    if (item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return Error(at, worldId, observed, "building_prefab_id_invalid");
                    TopologyInstance value; if (!TryRead(item, prefab.InstanceID, itemWorld, out value)) return Error(at, worldId, observed, "building_utility_topology_read_error");
                    bound.Add(item); observed.Add(value);
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                for (int i = 0; i < bound.Count; i++)
                {
                    BuildingComplete item = bound[i]; if (item == null || item.gameObject == null || item.gameObject.GetMyWorldId() != worldId) return Error(at, worldId, observed, "building_world_binding_changed");
                    KPrefabID prefab = item.GetComponent<KPrefabID>(); TopologyInstance current;
                    if (prefab == null || prefab.InstanceID != observed[i].InstanceId || !TryRead(item, prefab.InstanceID, worldId, out current)) return Error(at, worldId, observed, "building_utility_topology_read_error");
                    if (!Equals(observed[i], current)) return Error(at, worldId, observed, "building_utility_topology_changed_during_read");
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                observed.Sort(Compare); return Payload(at, worldId, true, observed, "observed", null);
            }
            catch { return Error(at, worldId, observed, "functional_building_utility_topology_read_error"); }
        }

        private static bool TryRead(BuildingComplete item, int instanceId, int worldId, out TopologyInstance value)
        {
            value = null; if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 || item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return false;
            value = new TopologyInstance { InstanceId = instanceId, WorldId = worldId, PrefabId = item.Def.PrefabID };
            if (!ReadSide(item, worldId, true, out value.Input) || !ReadSide(item, worldId, false, out value.Output)) { value = null; return false; }
            return true;
        }

        private static bool ReadSide(BuildingComplete item, int worldId, bool input, out TopologySide side)
        {
            side = new TopologySide { Declared = input ? item.Def.InputConduitType != ConduitType.None : item.Def.OutputConduitType != ConduitType.None };
            if (!side.Declared) return true;
            try
            {
                ConduitType type = input ? item.Def.InputConduitType : item.Def.OutputConduitType;
                int cell = input ? item.GetUtilityInputCell() : item.GetUtilityOutputCell(); side.Cell = cell;
                if (!Grid.IsValidCellInWorld(cell, worldId)) return false;
                IUtilityNetworkMgr mgr = type == ConduitType.Solid ? (Game.Instance == null ? null : Game.Instance.solidConduitSystem) : Conduit.GetNetworkManager(type);
                FlowUtilityNetwork network = mgr == null ? null : mgr.GetNetworkForCell(cell) as FlowUtilityNetwork;
                if (network == null) return true;
                if (network.sources == null || network.sinks == null || network.conduits == null) return false;
                side.NetworkId = network.id; side.SourceCount = network.sources.Count; side.SinkCount = network.sinks.Count; side.ConduitCount = network.conduitCount; side.HasSinks = network.HasSinks;
                return side.SourceCount >= 0 && side.SinkCount >= 0 && side.ConduitCount >= 0;
            }
            catch { side.NetworkId = null; return true; }
        }
        private static bool Equals(TopologyInstance a, TopologyInstance b) { return a != null && b != null && a.InstanceId == b.InstanceId && a.WorldId == b.WorldId && string.Equals(a.PrefabId, b.PrefabId, StringComparison.Ordinal) && Equals(a.Input, b.Input) && Equals(a.Output, b.Output); }
        private static bool Equals(TopologySide a, TopologySide b) { return a != null && b != null && a.Declared == b.Declared && a.Cell == b.Cell && a.NetworkId == b.NetworkId && a.SourceCount == b.SourceCount && a.SinkCount == b.SinkCount && a.ConduitCount == b.ConduitCount && a.HasSinks == b.HasSinks; }
        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer world, int worldId) { return manager != null && world != null && worldId >= 0 && ReferenceEquals(ClusterManager.Instance, manager) && ReferenceEquals(manager.activeWorld, world) && manager.activeWorldId == worldId && world.id == worldId; }
        private static int Compare(TopologyInstance a, TopologyInstance b) { int c = string.CompareOrdinal(a.PrefabId, b.PrefabId); return c != 0 ? c : a.InstanceId.CompareTo(b.InstanceId); }
        private static Dictionary<string, object> Error(DateTimeOffset at, int world, List<TopologyInstance> values, string reason) { return Payload(at, world, false, values, "error", reason); }
        private static Dictionary<string, object> Payload(DateTimeOffset at, int world, bool complete, List<TopologyInstance> values, string status, string reason)
        {
            List<object> records = new List<object>(); foreach (TopologyInstance v in values) records.Add(new Dictionary<string, object> { { "instance_id", v.InstanceId }, { "world_id", v.WorldId }, { "prefab_id", v.PrefabId }, { "input", Side(v.Input) }, { "output", Side(v.Output) } });
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema", Schema }, { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) }, { "world_id", world }, { "complete", complete }, { "instances", records }, { "_status", status }, { "_source", Source } }; if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason; return result;
        }
        private static Dictionary<string, object> Side(TopologySide s) { return new Dictionary<string, object> { { "declared", s.Declared }, { "cell", s.Declared ? (object)s.Cell : null }, { "network_id", s.NetworkId.HasValue ? (object)s.NetworkId.Value : null }, { "source_count", s.SourceCount.HasValue ? (object)s.SourceCount.Value : null }, { "sink_count", s.SinkCount.HasValue ? (object)s.SinkCount.Value : null }, { "conduit_count", s.ConduitCount.HasValue ? (object)s.ConduitCount.Value : null }, { "has_sinks", s.HasSinks.HasValue ? (object)s.HasSinks.Value : null } }; }
        private sealed class TopologyInstance { internal int InstanceId; internal int WorldId; internal string PrefabId; internal TopologySide Input; internal TopologySide Output; }
        private sealed class TopologySide { internal bool Declared; internal int Cell = -1; internal int? NetworkId; internal int? SourceCount; internal int? SinkCount; internal int? ConduitCount; internal bool? HasSinks; }
    }
}
