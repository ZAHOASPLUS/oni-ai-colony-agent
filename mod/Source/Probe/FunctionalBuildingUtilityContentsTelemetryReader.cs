using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>Reads instantaneous conduit contents without writing simulation state.</summary>
    internal static class FunctionalBuildingUtilityContentsTelemetryReader
    {
        internal const string Schema = "oni.functional_building_utility_contents.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+ConduitFlow.GetContents/SolidConduitFlow.GetContents";
        private const int EnumerationLimit = 10000;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset at = DateTimeOffset.UtcNow;
            List<ContentsInstance> values = new List<ContentsInstance>();
            List<BuildingComplete> bound = new List<BuildingComplete>();
            int worldId = -1;
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                WorldContainer world = manager == null ? null : manager.activeWorld;
                worldId = manager == null ? -1 : manager.activeWorldId;
                if (!Bound(manager, world, worldId)) return Error(at, worldId, values, "active_world_binding_invalid");
                IEnumerable<BuildingComplete> items;
                try { items = Components.BuildingCompletes.GetWorldItems(worldId); } catch { return Error(at, worldId, values, "building_registry_error"); }
                if (items == null) return Error(at, worldId, values, "building_registry_unavailable");
                HashSet<int> ids = new HashSet<int>(); int count = 0;
                foreach (BuildingComplete item in items)
                {
                    if (count++ >= EnumerationLimit) return Error(at, worldId, values, "building_registry_limit");
                    if (item == null || item.gameObject == null) return Error(at, worldId, values, "building_identity_invalid");
                    int itemWorld; try { itemWorld = item.gameObject.GetMyWorldId(); } catch { return Error(at, worldId, values, "building_world_read_error"); }
                    if (itemWorld != worldId) return Error(at, worldId, values, "building_world_mismatch");
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !ids.Add(prefab.InstanceID)) return Error(at, worldId, values, "building_instance_id_invalid");
                    if (item.Def == null || string.IsNullOrEmpty(item.Def.PrefabID)) return Error(at, worldId, values, "building_prefab_id_invalid");
                    ContentsInstance v; if (!TryRead(item, prefab.InstanceID, itemWorld, out v)) return Error(at, worldId, values, "building_utility_contents_read_error");
                    bound.Add(item); values.Add(v);
                }
                if (!Bound(manager, world, worldId)) return Error(at, worldId, values, "active_world_changed_during_read");
                for (int i = 0; i < bound.Count; i++)
                {
                    BuildingComplete item = bound[i]; if (item == null || item.gameObject == null || item.gameObject.GetMyWorldId() != worldId) return Error(at, worldId, values, "building_world_binding_changed");
                    KPrefabID prefab = item.GetComponent<KPrefabID>(); ContentsInstance current;
                    if (prefab == null || prefab.InstanceID != values[i].InstanceId || !TryRead(item, prefab.InstanceID, worldId, out current)) return Error(at, worldId, values, "building_utility_contents_read_error");
                    if (!Equals(values[i], current)) return Error(at, worldId, values, "building_utility_contents_changed_during_read");
                }
                values.Sort(Compare); return Payload(at, worldId, true, values, "observed", null);
            }
            catch { return Error(at, worldId, values, "functional_building_utility_contents_read_error"); }
        }

        private static bool TryRead(BuildingComplete item, int id, int worldId, out ContentsInstance value)
        {
            value = null; if (item == null || item.Def == null || id <= 0) return false;
            value = new ContentsInstance { InstanceId = id, WorldId = worldId, PrefabId = item.Def.PrefabID };
            if (!ReadSide(item, worldId, true, out value.Input) || !ReadSide(item, worldId, false, out value.Output)) { value = null; return false; }
            return true;
        }
        private static bool ReadSide(BuildingComplete item, int worldId, bool input, out ContentsSide side)
        {
            ConduitType type = input ? item.Def.InputConduitType : item.Def.OutputConduitType;
            side = new ContentsSide { Declared = type != ConduitType.None };
            if (!side.Declared) return true;
            try
            {
                int cell = input ? item.GetUtilityInputCell() : item.GetUtilityOutputCell(); side.Cell = cell;
                if (!Grid.IsValidCellInWorld(cell, worldId)) return false;
                if (type == ConduitType.Solid)
                {
                    if (Game.Instance == null || Game.Instance.solidConduitFlow == null) return true;
                    SolidConduitFlow.ConduitContents c = Game.Instance.solidConduitFlow.GetContents(cell);
                    side.ContentKind = "solid"; side.SolidPresent = c.pickupableHandle.IsValid(); return true;
                }
                ConduitFlow flow = type == ConduitType.Gas ? Game.Instance?.gasConduitFlow : Game.Instance?.liquidConduitFlow;
                if (flow == null) return true;
                ConduitFlow.ConduitContents contents = flow.GetContents(cell);
                if (float.IsNaN(contents.mass) || float.IsInfinity(contents.mass) || contents.mass < 0f || float.IsNaN(contents.temperature) || float.IsInfinity(contents.temperature)) return false;
                side.ContentKind = type == ConduitType.Gas ? "gas" : "liquid"; side.Element = contents.element.ToString(); side.Mass = contents.mass; side.Temperature = contents.temperature; side.DiseaseCount = contents.diseaseCount; return true;
            }
            catch { side.ContentKind = null; return true; }
        }
        private static bool Equals(ContentsInstance a, ContentsInstance b) { return a != null && b != null && a.InstanceId == b.InstanceId && a.WorldId == b.WorldId && string.Equals(a.PrefabId, b.PrefabId, StringComparison.Ordinal) && Equals(a.Input, b.Input) && Equals(a.Output, b.Output); }
        private static bool Equals(ContentsSide a, ContentsSide b) { return a != null && b != null && a.Declared == b.Declared && a.Cell == b.Cell && a.ContentKind == b.ContentKind && a.Element == b.Element && a.Mass == b.Mass && a.Temperature == b.Temperature && a.DiseaseCount == b.DiseaseCount && a.SolidPresent == b.SolidPresent; }
        private static bool Bound(ClusterManager m, WorldContainer w, int id) { return m != null && w != null && id >= 0 && ReferenceEquals(ClusterManager.Instance, m) && ReferenceEquals(m.activeWorld, w) && m.activeWorldId == id && w.id == id; }
        private static int Compare(ContentsInstance a, ContentsInstance b) { int c = string.CompareOrdinal(a.PrefabId, b.PrefabId); return c != 0 ? c : a.InstanceId.CompareTo(b.InstanceId); }
        private static Dictionary<string, object> Error(DateTimeOffset at, int world, List<ContentsInstance> v, string reason) { return Payload(at, world, false, v, "error", reason); }
        private static Dictionary<string, object> Payload(DateTimeOffset at, int world, bool complete, List<ContentsInstance> values, string status, string reason)
        {
            List<object> records = new List<object>(); foreach (ContentsInstance v in values) records.Add(new Dictionary<string, object> { { "instance_id", v.InstanceId }, { "world_id", v.WorldId }, { "prefab_id", v.PrefabId }, { "input", Side(v.Input) }, { "output", Side(v.Output) } });
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema", Schema }, { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) }, { "world_id", world }, { "complete", complete }, { "instances", records }, { "_status", status }, { "_source", Source } }; if (!string.IsNullOrEmpty(reason)) result["_reason"] = reason; return result;
        }
        private static Dictionary<string, object> Side(ContentsSide s) { return new Dictionary<string, object> { { "declared", s.Declared }, { "cell", s.Declared ? (object)s.Cell : null }, { "content_kind", s.ContentKind }, { "element", s.Element }, { "mass", s.Mass.HasValue ? (object)s.Mass.Value : null }, { "temperature", s.Temperature.HasValue ? (object)s.Temperature.Value : null }, { "disease_count", s.DiseaseCount.HasValue ? (object)s.DiseaseCount.Value : null }, { "solid_present", s.SolidPresent.HasValue ? (object)s.SolidPresent.Value : null } }; }
        private sealed class ContentsInstance { internal int InstanceId; internal int WorldId; internal string PrefabId; internal ContentsSide Input; internal ContentsSide Output; }
        private sealed class ContentsSide { internal bool Declared; internal int Cell = -1; internal string ContentKind; internal string Element; internal float? Mass; internal float? Temperature; internal int? DiseaseCount; internal bool? SolidPresent; }
    }
}
