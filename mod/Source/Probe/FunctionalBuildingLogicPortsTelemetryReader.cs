using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>Reads declared LogicPorts and connection membership only.</summary>
    internal static class FunctionalBuildingLogicPortsTelemetryReader
    {
        internal const string Schema = "oni.functional_building_logic_ports.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+LogicPorts.inputPortInfo/outputPortInfo+GetPortCell+IsPortConnected";
        private const int EnumerationLimit = 10000;
        private const int PortLimit = 256;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset at = DateTimeOffset.UtcNow;
            List<LogicInstance> observed = new List<LogicInstance>();
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
                    LogicInstance value;
                    if (!TryRead(item, prefab.InstanceID, itemWorld, out value)) return Error(at, worldId, observed, "building_logic_ports_read_error");
                    bound.Add(item); observed.Add(value);
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                for (int i = 0; i < bound.Count; i++)
                {
                    BuildingComplete item = bound[i];
                    if (item == null || item.gameObject == null) return Error(at, worldId, observed, "building_world_binding_changed");
                    int itemWorld;
                    try { itemWorld = item.gameObject.GetMyWorldId(); } catch { return Error(at, worldId, observed, "building_world_binding_changed"); }
                    if (itemWorld != worldId) return Error(at, worldId, observed, "building_world_binding_changed");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID != observed[i].InstanceId) return Error(at, worldId, observed, "building_identity_changed");
                    LogicInstance current;
                    if (!TryRead(item, prefab.InstanceID, itemWorld, out current)) return Error(at, worldId, observed, "building_logic_ports_read_error");
                    if (!Equals(observed[i], current)) return Error(at, worldId, observed, "building_logic_ports_changed_during_read");
                }
                if (!WorldBindingMatches(manager, world, worldId)) return Error(at, worldId, observed, "active_world_changed_during_read");
                observed.Sort(Compare);
                return Payload(at, worldId, true, observed, "observed", null);
            }
            catch { return Error(at, worldId, observed, "functional_building_logic_ports_read_error"); }
        }

        private static bool TryRead(BuildingComplete item, int instanceId, int worldId, out LogicInstance value)
        {
            value = null;
            if (item == null || item.gameObject == null || instanceId <= 0 || worldId < 0 || item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return false;
            LogicPorts logic = null;
            try { logic = item.GetComponent<LogicPorts>(); } catch { logic = null; }
            List<LogicPortRecord> ports = new List<LogicPortRecord>();
            if (logic != null)
            {
                if (!ReadArray(logic, logic.inputPortInfo, "input", worldId, ports)) return false;
                if (!ReadArray(logic, logic.outputPortInfo, "output", worldId, ports)) return false;
            }
            value = new LogicInstance { InstanceId = instanceId, WorldId = worldId, PrefabId = item.Def.PrefabID, ComponentPresent = logic != null, Ports = ports };
            return true;
        }

        private static bool ReadArray(LogicPorts logic, LogicPorts.Port[] source, string direction, int worldId, List<LogicPortRecord> ports)
        {
            if (source == null) return true;
            if (ports.Count + source.Length > PortLimit) return false;
            HashSet<string> seen = new HashSet<string>();
            foreach (LogicPortRecord existing in ports) seen.Add(existing.Direction + "|" + existing.Id);
            foreach (LogicPorts.Port port in source)
            {
                string id = port.id.ToString();
                string signature = direction + "|" + id;
                if (string.IsNullOrEmpty(id) || !seen.Add(signature)) return false;
                int cell;
                bool? connected = null;
                try
                {
                    cell = logic.GetPortCell(port.id);
                    if (!Grid.IsValidCellInWorld(cell, worldId)) return false;
                    connected = logic.IsPortConnected(port.id);
                }
                catch { cell = -1; connected = null; }
                if (cell < 0) return false;
                ports.Add(new LogicPortRecord { Id = id, Direction = direction, Cell = cell, RequiresConnection = port.requiresConnection, IsConnected = connected });
            }
            return true;
        }

        private static bool Equals(LogicInstance a, LogicInstance b)
        {
            if (a == null || b == null || a.InstanceId != b.InstanceId || a.WorldId != b.WorldId || a.ComponentPresent != b.ComponentPresent || !string.Equals(a.PrefabId, b.PrefabId, StringComparison.Ordinal) || a.Ports.Count != b.Ports.Count) return false;
            for (int i = 0; i < a.Ports.Count; i++) { LogicPortRecord x = a.Ports[i], y = b.Ports[i]; if (x.Id != y.Id || x.Direction != y.Direction || x.Cell != y.Cell || x.RequiresConnection != y.RequiresConnection || x.IsConnected != y.IsConnected) return false; }
            return true;
        }
        private static bool WorldBindingMatches(ClusterManager manager, WorldContainer world, int worldId) { return manager != null && world != null && worldId >= 0 && ReferenceEquals(ClusterManager.Instance, manager) && ReferenceEquals(manager.activeWorld, world) && manager.activeWorldId == worldId && world.id == worldId; }
        private static int Compare(LogicInstance a, LogicInstance b) { int c = string.CompareOrdinal(a.PrefabId, b.PrefabId); return c != 0 ? c : a.InstanceId.CompareTo(b.InstanceId); }
        private static Dictionary<string, object> Error(DateTimeOffset at, int world, List<LogicInstance> values, string reason) { return Payload(at, world, false, values, "error", reason); }
        private static Dictionary<string, object> Payload(DateTimeOffset at, int world, bool complete, List<LogicInstance> values, string status, string reason)
        {
            List<object> records = new List<object>();
            foreach (LogicInstance value in values)
            {
                List<object> ports = new List<object>();
                foreach (LogicPortRecord port in value.Ports) ports.Add(new Dictionary<string, object> { { "id", port.Id }, { "direction", port.Direction }, { "cell", port.Cell }, { "requires_connection", port.RequiresConnection }, { "is_connected", port.IsConnected.HasValue ? (object)port.IsConnected.Value : null } });
                records.Add(new Dictionary<string, object> { { "instance_id", value.InstanceId }, { "world_id", value.WorldId }, { "prefab_id", value.PrefabId }, { "component_present", value.ComponentPresent }, { "ports", ports } });
            }
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema", Schema }, { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) }, { "world_id", world }, { "complete", complete }, { "instances", records }, { "_status", status }, { "_source", Source } };
            if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason;
            return result;
        }
        private sealed class LogicInstance { internal int InstanceId; internal int WorldId; internal string PrefabId; internal bool ComponentPresent; internal List<LogicPortRecord> Ports; }
        private sealed class LogicPortRecord { internal string Id; internal string Direction; internal int Cell; internal bool RequiresConnection; internal bool? IsConnected; }
    }
}
