using System.Collections.Generic;
using System.Linq;
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
            AddConfigurableOptions(go, entry);
            return entry;
        }

        // What configure can set here, so a player never has to guess an id.
        private static void AddConfigurableOptions(GameObject go, Dictionary<string, object> entry)
        {
            ApiBuildingSettings.Describe(go, entry);
            ComplexFabricator fabricator = go.GetComponent<ComplexFabricator>();
            if (fabricator != null)
            {
                entry["recipes"] = fabricator.GetRecipes().Select(recipe => (object)new Dictionary<string, object>
                {
                    { "id", recipe.id },
                    { "makes", DescribeElements(recipe.results) },
                    { "needs", DescribeElements(recipe.ingredients) },
                    { "seconds", ApiValues.Number(recipe.time, 0) },
                    { "queued", fabricator.GetRecipeQueueCount(recipe) }
                }).ToList();
            }

            SingleEntityReceptacle receptacle = go.GetComponent<SingleEntityReceptacle>();
            if (receptacle != null)
            {
                entry["occupant"] = receptacle.Occupant == null
                    ? null
                    : receptacle.Occupant.GetComponent<KPrefabID>().PrefabTag.Name;
                entry["requested"] = receptacle.requestedEntityTag.IsValid ? receptacle.requestedEntityTag.Name : null;
                entry["seeds_in_stock"] = ClusterManager.Instance.activeWorld.worldInventory.GetAccessibleAmounts()
                    .Where(amount => amount.Value > 0f)
                    .Select(amount => amount.Key)
                    .Where(tag => CanHold(receptacle, tag))
                    .Select(tag => tag.Name)
                    .ToList();
            }

            TreeFilterable filterable = go.GetComponent<TreeFilterable>();
            if (filterable != null)
            {
                entry["storage_filter"] = filterable.GetTags().Select(tag => tag.Name).ToList();
            }

            Dictionary<string, object> rocket = ApiRocketTools.DescribeRocket(go);
            if (rocket != null)
            {
                entry["rocket"] = rocket;
            }
        }

        private static bool CanHold(SingleEntityReceptacle receptacle, Tag tag)
        {
            GameObject prefab = Assets.TryGetPrefab(tag);
            return prefab != null &&
                receptacle.possibleDepositObjectTags.Any(prefab.HasTag) &&
                receptacle.IsValidEntity(prefab);
        }

        private static List<object> DescribeElements(IEnumerable<ComplexRecipe.RecipeElement> elements)
        {
            return elements.Select(element => (object)new Dictionary<string, object>
            {
                { "item", element.material.Name },
                { "amount", ApiValues.Number(element.amount, 1) }
            }).ToList();
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
