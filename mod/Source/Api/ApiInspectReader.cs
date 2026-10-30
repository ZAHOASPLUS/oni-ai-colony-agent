using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>One cell: its contents, and every building and loose item in it.</summary>
    internal static class ApiInspectReader
    {
        private const int StatusItemLimit = 12;
        private const int StoredItemLimit = 20;
        private const int LooseItemLimit = 20;

        internal static object Read(JObject args)
        {
            int x = ApiArgs.Int(args, "x", int.MinValue);
            int y = ApiArgs.Int(args, "y", int.MinValue);
            if (x == int.MinValue || y == int.MinValue)
            {
                throw new ApiException("invalid_argument", "x and y are required.");
            }
            int cell = ApiValues.CheckedCell(x, y);
            if (!Grid.IsVisible(cell))
            {
                return new Dictionary<string, object>
                {
                    { "x", x },
                    { "y", y },
                    { "revealed", false }
                };
            }

            Element element = Grid.Element[cell];
            return new Dictionary<string, object>
            {
                { "x", x },
                { "y", y },
                { "revealed", true },
                { "world", (int)Grid.WorldIdx[cell] },
                { "element", element.id.ToString() },
                { "state", element.IsSolid ? "solid" : element.IsLiquid ? "liquid" : element.IsGas ? "gas" : "vacuum" },
                { "mass_kg", ApiValues.Number(Grid.Mass[cell], 2) },
                { "temperature_c", element.IsVacuum ? null : ApiValues.Celsius(Grid.Temperature[cell]) },
                { "germs", Grid.DiseaseCount[cell] },
                { "buildings", ReadBuildings(cell) },
                { "items", ReadLooseItems(cell) }
            };
        }

        private static List<object> ReadBuildings(int cell)
        {
            List<object> buildings = new List<object>();
            HashSet<GameObject> seen = new HashSet<GameObject>();
            for (int layer = 0; layer < (int)ObjectLayer.NumLayers; layer++)
            {
                GameObject go = Grid.Objects[cell, layer];
                if (go == null || !seen.Add(go) || go.GetComponent<Building>() == null)
                {
                    continue;
                }
                buildings.Add(ReadBuilding(go));
            }
            return buildings;
        }

        private static Dictionary<string, object> ReadBuilding(GameObject go)
        {
            Building building = go.GetComponent<Building>();
            KPrefabID prefab = go.GetComponent<KPrefabID>();
            Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(go));
            entry["id"] = prefab.InstanceID;
            entry["def"] = building.Def.PrefabID;
            entry["size"] = new[] { building.Def.WidthInCells, building.Def.HeightInCells };
            entry["blueprint"] = building is BuildingUnderConstruction;
            PrimaryElement material = go.GetComponent<PrimaryElement>();
            if (material != null)
            {
                entry["material"] = material.ElementID.ToString();
                entry["temperature_c"] = ApiValues.Celsius(material.Temperature);
            }
            Operational operational = go.GetComponent<Operational>();
            if (operational != null)
            {
                entry["operational"] = operational.IsOperational;
                entry["active"] = operational.IsActive;
            }
            entry["status"] = ApiStatusReader.ReadStatusItems(go, StatusItemLimit);

            List<object> stored = new List<object>();
            foreach (Storage storage in go.GetComponents<Storage>())
            {
                foreach (GameObject item in storage.items)
                {
                    if (item != null && stored.Count < StoredItemLimit)
                    {
                        stored.Add(DescribeItem(item));
                    }
                }
            }
            if (stored.Count > 0)
            {
                entry["stored"] = stored;
            }
            return entry;
        }

        private static List<object> ReadLooseItems(int cell)
        {
            List<object> items = new List<object>();
            GameObject first = Grid.Objects[cell, (int)ObjectLayer.Pickupables];
            Pickupable pickupable = first == null ? null : first.GetComponent<Pickupable>();
            ObjectLayerListItem node = pickupable == null ? null : pickupable.objectLayerListItem;
            while (node != null && items.Count < LooseItemLimit)
            {
                if (node.gameObject != null)
                {
                    items.Add(DescribeItem(node.gameObject));
                }
                node = node.nextItem;
            }
            return items;
        }

        private static Dictionary<string, object> DescribeItem(GameObject item)
        {
            Dictionary<string, object> entry = new Dictionary<string, object>
            {
                { "item", item.GetComponent<KPrefabID>().PrefabTag.Name }
            };
            PrimaryElement element = item.GetComponent<PrimaryElement>();
            if (element != null)
            {
                entry["mass_kg"] = ApiValues.Number(element.Mass, 2);
                entry["temperature_c"] = ApiValues.Celsius(element.Temperature);
            }
            return entry;
        }
    }
}
