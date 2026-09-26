using System;
using System.Collections.Generic;
using System.Globalization;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads only the port declarations carried by completed buildings in the
    /// active world.  This is a declaration surface: it never inspects
    /// conduit/logic networks, occupancy, or connection state.
    /// </summary>
    internal static class FunctionalBuildingPortsTelemetryReader
    {
        internal const string Schema = "oni.functional_building_ports.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+BuildingDef.RequiresPowerInput/Output+BuildingDef.InputConduitType/OutputConduitType+BuildingDef.UseHighEnergyParticleInputPort/OutputPort+Building.Orientation+Building.GetPowerInputCell/GetPowerOutputCell/GetUtilityInputCell/GetUtilityOutputCell/GetHighEnergyParticleInputCell/GetHighEnergyParticleOutputCell";

        private const int EnumerationLimit = 10000;
        private const int PortLimit = 32;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<PortInstance> observedInstances = new List<PortInstance>();
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

                    PortInstance ports;
                    if (!TryReadPorts(item, building, prefab.InstanceID, itemWorldId, out ports, out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }

                    boundItems.Add(item);
                    observedInstances.Add(ports);
                }

                if (!WorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    return Error(capturedAt, activeWorldId, observedInstances, "active_world_changed_during_read");
                }

                // Re-read every item used as evidence.  A registry mutation,
                // world switch, orientation change, or definition change is
                // an unknown snapshot rather than a partially trusted result.
                for (int index = 0; index < boundItems.Count; index++)
                {
                    BuildingComplete item = boundItems[index];
                    if (item == null || item.gameObject == null)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }
                    try
                    {
                        if (item.gameObject.GetMyWorldId() != activeWorldId)
                        {
                            return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                        }
                    }
                    catch
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_world_binding_changed");
                    }

                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    Building building = item.GetComponent<Building>();
                    if (prefab == null || building == null ||
                        prefab.InstanceID != observedInstances[index].InstanceId)
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_identity_changed");
                    }

                    PortInstance current;
                    if (!TryReadPorts(item, building, prefab.InstanceID, activeWorldId, out current, out string reason))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, reason);
                    }
                    if (!PortsEquals(observedInstances[index], current))
                    {
                        return Error(capturedAt, activeWorldId, observedInstances, "building_ports_changed_during_read");
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
                return Error(capturedAt, activeWorldId, observedInstances, "functional_building_ports_read_error");
            }
        }

        private static bool TryReadPorts(
            BuildingComplete item,
            Building building,
            int instanceId,
            int worldId,
            out PortInstance instance,
            out string reason)
        {
            instance = null;
            reason = "building_ports_read_error";
            if (item == null || building == null || worldId < 0 || instanceId <= 0)
            {
                reason = "building_ports_identity_invalid";
                return false;
            }

            try
            {
                BuildingDef def = item.Def;
                string prefabId = def?.PrefabID;
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

                List<PortRecord> ports = new List<PortRecord>();
                HashSet<string> signatures = new HashSet<string>(StringComparer.Ordinal);
                if (def.RequiresPowerInput && !AddPort(
                    ports, signatures, "power", "input", building.GetPowerInputCell(), null, worldId, out reason))
                {
                    return false;
                }
                if (def.RequiresPowerOutput && !AddPort(
                    ports, signatures, "power", "output", building.GetPowerOutputCell(), null, worldId, out reason))
                {
                    return false;
                }
                if (def.InputConduitType != ConduitType.None)
                {
                    string conduitType;
                    if (!TryConduitType(def.InputConduitType, out conduitType))
                    {
                        reason = "building_input_conduit_type_invalid";
                        return false;
                    }
                    if (!AddPort(
                        ports, signatures, "conduit", "input", building.GetUtilityInputCell(), conduitType, worldId, out reason))
                    {
                        return false;
                    }
                }
                if (def.OutputConduitType != ConduitType.None)
                {
                    string conduitType;
                    if (!TryConduitType(def.OutputConduitType, out conduitType))
                    {
                        reason = "building_output_conduit_type_invalid";
                        return false;
                    }
                    if (!AddPort(
                        ports, signatures, "conduit", "output", building.GetUtilityOutputCell(), conduitType, worldId, out reason))
                    {
                        return false;
                    }
                }
                if (def.UseHighEnergyParticleInputPort && !AddPort(
                    ports, signatures, "high_energy_particle", "input", building.GetHighEnergyParticleInputCell(), null, worldId, out reason))
                {
                    return false;
                }
                if (def.UseHighEnergyParticleOutputPort && !AddPort(
                    ports, signatures, "high_energy_particle", "output", building.GetHighEnergyParticleOutputCell(), null, worldId, out reason))
                {
                    return false;
                }

                ports.Sort(ComparePorts);
                instance = new PortInstance
                {
                    InstanceId = instanceId,
                    WorldId = worldId,
                    PrefabId = prefabId,
                    Orientation = orientation,
                    Ports = ports
                };
                return true;
            }
            catch
            {
                instance = null;
                reason = "building_ports_read_error";
                return false;
            }
        }

        private static bool AddPort(
            List<PortRecord> ports,
            HashSet<string> signatures,
            string kind,
            string direction,
            int cell,
            string type,
            int worldId,
            out string reason)
        {
            reason = "building_port_read_error";
            if (ports.Count >= PortLimit)
            {
                reason = "building_ports_too_many";
                return false;
            }
            if (!Grid.IsValidCellInWorld(cell, worldId))
            {
                reason = "building_port_cell_invalid";
                return false;
            }
            string signature = string.Concat(kind, "|", direction, "|", cell.ToString(CultureInfo.InvariantCulture), "|", type ?? "");
            if (!signatures.Add(signature))
            {
                reason = "building_port_duplicate";
                return false;
            }
            ports.Add(new PortRecord
            {
                Kind = kind,
                Direction = direction,
                Cell = cell,
                Type = type,
                ConnectionStatus = "unknown"
            });
            return true;
        }

        private static bool TryConduitType(ConduitType value, out string type)
        {
            switch (value)
            {
                case ConduitType.Gas:
                    type = "gas";
                    return true;
                case ConduitType.Liquid:
                    type = "liquid";
                    return true;
                case ConduitType.Solid:
                    type = "solid";
                    return true;
                default:
                    type = null;
                    return false;
            }
        }

        private static bool PortsEquals(PortInstance left, PortInstance right)
        {
            if (left == null || right == null || left.InstanceId != right.InstanceId ||
                left.WorldId != right.WorldId ||
                !string.Equals(left.PrefabId, right.PrefabId, StringComparison.Ordinal) ||
                !string.Equals(left.Orientation, right.Orientation, StringComparison.Ordinal) ||
                left.Ports.Count != right.Ports.Count)
            {
                return false;
            }
            for (int index = 0; index < left.Ports.Count; index++)
            {
                PortRecord a = left.Ports[index];
                PortRecord b = right.Ports[index];
                if (!string.Equals(a.Kind, b.Kind, StringComparison.Ordinal) ||
                    !string.Equals(a.Direction, b.Direction, StringComparison.Ordinal) ||
                    a.Cell != b.Cell ||
                    !string.Equals(a.Type, b.Type, StringComparison.Ordinal) ||
                    !string.Equals(a.ConnectionStatus, b.ConnectionStatus, StringComparison.Ordinal))
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

        private static int CompareInstances(PortInstance left, PortInstance right)
        {
            int prefabCompare = string.CompareOrdinal(left.PrefabId, right.PrefabId);
            return prefabCompare != 0 ? prefabCompare : left.InstanceId.CompareTo(right.InstanceId);
        }

        private static int ComparePorts(PortRecord left, PortRecord right)
        {
            int kindCompare = string.CompareOrdinal(left.Kind, right.Kind);
            if (kindCompare != 0)
            {
                return kindCompare;
            }
            int directionCompare = string.CompareOrdinal(left.Direction, right.Direction);
            if (directionCompare != 0)
            {
                return directionCompare;
            }
            int cellCompare = left.Cell.CompareTo(right.Cell);
            if (cellCompare != 0)
            {
                return cellCompare;
            }
            return string.CompareOrdinal(left.Type ?? string.Empty, right.Type ?? string.Empty);
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset capturedAt,
            int activeWorldId,
            List<PortInstance> instances,
            string reason)
        {
            return Payload(capturedAt, activeWorldId, false, instances, "error", reason);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset capturedAt,
            int activeWorldId,
            bool complete,
            List<PortInstance> instances,
            string status,
            string reason)
        {
            List<object> jsonInstances = new List<object>();
            foreach (PortInstance instance in instances)
            {
                List<object> jsonPorts = new List<object>();
                foreach (PortRecord port in instance.Ports)
                {
                    jsonPorts.Add(new Dictionary<string, object>
                    {
                        { "kind", port.Kind },
                        { "direction", port.Direction },
                        { "cell", port.Cell },
                        { "type", port.Type },
                        { "connection_status", port.ConnectionStatus }
                    });
                }
                jsonInstances.Add(new Dictionary<string, object>
                {
                    { "instance_id", instance.InstanceId },
                    { "world_id", instance.WorldId },
                    { "prefab_id", instance.PrefabId },
                    { "orientation", instance.Orientation },
                    { "ports", jsonPorts }
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

        private sealed class PortInstance
        {
            internal int InstanceId;
            internal int WorldId;
            internal string PrefabId;
            internal string Orientation;
            internal List<PortRecord> Ports;
        }

        private sealed class PortRecord
        {
            internal string Kind;
            internal string Direction;
            internal int Cell;
            internal string Type;
            internal string ConnectionStatus;
        }
    }
}
