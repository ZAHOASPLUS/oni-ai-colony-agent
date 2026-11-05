using System;
using Klei.AI;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>Questions a player asks while planning: what is there, and where.</summary>
    internal static class ApiQueries
    {
        private const int BuildingLimit = 200;
        private const int ResourceLimit = 80;
        private const int FindResults = 5;
        private const int MaxFindResults = 100;
        private const int StatusLines = 4;

        internal static object ListBuildings(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            string filter = ApiArgs.Text(args, "def");
            bool problemsOnly = ApiArgs.Flag(args, "problems_only", false);
            bool structural = ApiArgs.Flag(args, "include_structural", false);
            List<object> buildings = new List<object>();
            int matched = 0;
            foreach (BuildingComplete building in Components.BuildingCompletes.Items)
            {
                if (building == null || building.GetMyWorldId() != world.id)
                {
                    continue;
                }
                BuildingDef def = building.Def;
                if (filter != null
                    ? def.PrefabID.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0
                    : !structural && (def.isUtility || def.IsFoundation || def.PrefabID == "Ladder"))
                {
                    continue;
                }
                Operational operational = building.GetComponent<Operational>();
                if (problemsOnly && (operational == null || operational.IsOperational))
                {
                    continue;
                }
                matched++;
                if (buildings.Count >= BuildingLimit)
                {
                    continue;
                }
                Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(building));
                entry["id"] = building.GetComponent<KPrefabID>().InstanceID;
                entry["def"] = def.PrefabID;
                if (operational != null)
                {
                    entry["operational"] = operational.IsOperational;
                    entry["status"] = ApiStatusReader.ReadStatusItems(building.gameObject, StatusLines);
                }
                buildings.Add(entry);
            }
            return new Dictionary<string, object>
            {
                { "matched", matched },
                { "buildings", buildings }
            };
        }

        internal static object ReadResources(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            // The inventory also counts by category tag, such as Metal or Pickupable.
            IEnumerable<KeyValuePair<Tag, float>> amounts = world.worldInventory.GetAccessibleAmounts()
                .Where(amount => amount.Value > 0f && Assets.TryGetPrefab(amount.Key) != null)
                .OrderByDescending(amount => amount.Value);
            Dictionary<string, object> resources = new Dictionary<string, object>();
            foreach (KeyValuePair<Tag, float> amount in amounts.Take(ResourceLimit))
            {
                resources[amount.Key.Name] = ApiValues.Number(amount.Value, 1);
            }
            AddPrivateStorageResources(world, resources);
            return new Dictionary<string, object>
            {
                { "note", "Reachable stock in kg, largest first." },
                { "resources", resources }
            };
        }

        private static void AddPrivateStorageResources(WorldContainer world, Dictionary<string, object> resources)
        {
            Dictionary<string, float> stored = new Dictionary<string, float>(StringComparer.Ordinal);
            foreach (BuildingComplete building in Components.BuildingCompletes.Items)
            {
                if (building == null || building.GetMyWorldId() != world.id)
                {
                    continue;
                }
                foreach (Storage storage in building.GetComponents<Storage>())
                {
                    foreach (GameObject item in storage.items)
                    {
                        if (item == null)
                        {
                            continue;
                        }
                        KPrefabID prefab = item.GetComponent<KPrefabID>();
                        PrimaryElement element = item.GetComponent<PrimaryElement>();
                        if (prefab == null || element == null || element.Mass <= 0f ||
                            float.IsNaN(element.Mass) || float.IsInfinity(element.Mass) ||
                            !prefab.HasTag(GameTags.StoredPrivate))
                        {
                            continue;
                        }
                        string name = prefab.PrefabTag.Name;
                        if (string.IsNullOrEmpty(name))
                        {
                            continue;
                        }
                        float current;
                        stored.TryGetValue(name, out current);
                        stored[name] = current + element.Mass;
                    }
                }
            }

            foreach (KeyValuePair<string, float> amount in stored)
            {
                object existing;
                if (resources.TryGetValue(amount.Key, out existing) && existing is IConvertible)
                {
                    resources[amount.Key] = ApiValues.Number(
                        Convert.ToSingle(existing) + amount.Value,
                        1);
                }
                else if (!resources.ContainsKey(amount.Key))
                {
                    resources[amount.Key] = ApiValues.Number(amount.Value, 1);
                }
            }
        }

        internal static object Find(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            string kind = ApiArgs.Text(args, "kind");
            string query = ApiArgs.Text(args, "query");
            Vector2I origin = ReadOrigin(args);

            List<Dictionary<string, object>> found;
            switch (kind)
            {
                case "element":
                    found = FindElement(world, query);
                    break;
                case "item":
                    found = FindItems(world, query);
                    break;
                case "building":
                    found = FindBuildings(world, query);
                    break;
                case "plant":
                    found = FindPlants(world, query);
                    break;
                case "geyser":
                    found = FindGeysers(world);
                    break;
                case "critter":
                    found = ApiCritters.Find(world, query);
                    break;
                default:
                    throw new ApiException("invalid_argument", "kind must be element, item, building, plant, geyser or critter.");
            }

            return new Dictionary<string, object>
            {
                { "near", new[] { origin.x, origin.y } },
                { "total", found.Count },
                {
                    "nearest",
                    found
                        .OrderBy(entry => Distance(entry, origin))
                        .Take(Math.Min(ApiArgs.Int(args, "limit", FindResults), MaxFindResults))
                        .ToList()
                }
            };
        }

        private static long Distance(Dictionary<string, object> entry, Vector2I origin)
        {
            long dx = (int)entry["x"] - origin.x;
            long dy = (int)entry["y"] - origin.y;
            return dx * dx + dy * dy;
        }

        private static Vector2I ReadOrigin(JObject args)
        {
            MinionIdentity first = Components.LiveMinionIdentities.Items.FirstOrDefault(identity => identity != null);
            int x = 0;
            int y = 0;
            if (first != null)
            {
                Grid.CellToXY(Grid.PosToCell(first), out x, out y);
            }
            return new Vector2I(ApiArgs.Int(args, "x", x), ApiArgs.Int(args, "y", y));
        }

        private static List<Dictionary<string, object>> FindElement(WorldContainer world, string query)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            int xMin = (int)world.minimumBounds.x;
            int xMax = (int)world.maximumBounds.x;
            int yMin = (int)world.minimumBounds.y;
            int yMax = (int)world.maximumBounds.y;
            for (int y = yMin; y <= yMax; y++)
            {
                for (int x = xMin; x <= xMax; x++)
                {
                    int cell = Grid.XYToCell(x, y);
                    if (Grid.IsVisible(cell) &&
                        string.Equals(Grid.Element[cell].id.ToString(), query, StringComparison.OrdinalIgnoreCase))
                    {
                        Dictionary<string, object> entry = ApiValues.Xy(cell);
                        entry["kg"] = ApiValues.Number(Grid.Mass[cell], 1);
                        found.Add(entry);
                    }
                }
            }
            return found;
        }

        private static List<Dictionary<string, object>> FindItems(WorldContainer world, string query)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            ICollection<Pickupable> items = world.worldInventory.GetPickupables(new Tag(query));
            if (items == null)
            {
                return found;
            }
            foreach (Pickupable item in items)
            {
                if (item != null)
                {
                    Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(item));
                    entry["item"] = item.KPrefabID.PrefabTag.Name;
                    entry["kg"] = ApiValues.Number(item.TotalAmount, 1);
                    found.Add(entry);
                }
            }
            return found;
        }

        private static List<Dictionary<string, object>> FindBuildings(WorldContainer world, string query)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            foreach (BuildingComplete building in Components.BuildingCompletes.Items)
            {
                if (building != null &&
                    building.GetMyWorldId() == world.id &&
                    string.Equals(building.Def.PrefabID, query, StringComparison.OrdinalIgnoreCase))
                {
                    Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(building));
                    entry["id"] = building.GetComponent<KPrefabID>().InstanceID;
                    found.Add(entry);
                }
            }
            return found;
        }

        private static List<Dictionary<string, object>> FindPlants(WorldContainer world, string query)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            foreach (HarvestDesignatable plant in Components.HarvestDesignatables.Items)
            {
                if (plant == null || plant.GetMyWorldId() != world.id)
                {
                    continue;
                }
                string name = plant.GetComponent<KPrefabID>().PrefabTag.Name;
                if (string.IsNullOrEmpty(query) || name.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(plant));
                    entry["plant"] = name;
                    entry["ready"] = plant.CanBeHarvested();
                    entry["harvest_when_ready"] = plant.HarvestWhenReady;
                    AmountInstance maturity = Db.Get().Amounts.Maturity.Lookup(plant.gameObject);
                    if (maturity != null)
                    {
                        entry["growth_pct"] = ApiValues.Number(100f * maturity.value / maturity.GetMax(), 0);
                    }
                    WiltCondition wilt = plant.GetComponent<WiltCondition>();
                    entry["stifled"] = wilt != null && wilt.IsWilting();
                    entry["status"] = ApiStatusReader.ReadStatusItems(plant.gameObject, 5);
                    found.Add(entry);
                }
            }
            return found;
        }

        // Only what a player can see: revealed geysers, and their rates once studied.
        private static List<Dictionary<string, object>> FindGeysers(WorldContainer world)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            foreach (Geyser geyser in Components.Geysers.GetItems(world.id))
            {
                if (geyser == null || !Grid.IsVisible(Grid.PosToCell(geyser)))
                {
                    continue;
                }
                GeyserConfigurator.GeyserType type = geyser.configuration.geyserType;
                Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(geyser));
                entry["geyser"] = type.id;
                entry["element"] = type.element.ToString();
                entry["temperature_c"] = ApiValues.Number(type.temperature - 273.15f, 1);
                ApiGeysers.Describe(geyser, entry);
                found.Add(entry);
            }
            return found;
        }

        // The stage goal's numbers, one row per finished cycle, so a player can
        // judge a trend instead of one moment.
        internal static object ReadProgress(JObject args)
        {
            return new Dictionary<string, object>
            {
                { "now", ApiStatusReader.ReadProgressRow() },
                { "duplicants_lost_this_session", ApiEvents.DuplicantsLost },
                { "cycles_this_session", ApiEvents.CycleLog },
                { "imperatives", ReadImperatives() },
                { "achievements_unlocked", ReadUnlockedAchievements() }
            };
        }

        private static List<Dictionary<string, object>> ReadImperatives()
        {
            List<Dictionary<string, object>> imperatives = new List<Dictionary<string, object>>();
            ColonyAchievementTracker tracker = SaveGame.Instance?.ColonyAchievementTracker;
            if (tracker == null || Db.Get()?.ColonyAchievements?.resources == null)
            {
                return imperatives;
            }
            foreach (Database.ColonyAchievement achievement in Db.Get().ColonyAchievements.resources)
            {
                if (achievement == null || !achievement.isVictoryCondition || !achievement.IsValidForSave())
                {
                    continue;
                }
                ColonyAchievementStatus status = null;
                if (tracker.achievements != null)
                {
                    tracker.achievements.TryGetValue(achievement.Id, out status);
                }
                bool achieved = status != null && status.success && !status.failed;
                List<Dictionary<string, object>> requirements = new List<Dictionary<string, object>>();
                IEnumerable<Database.ColonyAchievementRequirement> reqList =
                    status != null ? status.Requirements : achievement.requirementChecklist;
                if (reqList != null)
                {
                    foreach (Database.ColonyAchievementRequirement req in reqList)
                    {
                        if (req == null)
                        {
                            continue;
                        }
                        bool ok = achieved || req.Success();
                        bool failed = req.Fail();
                        requirements.Add(new Dictionary<string, object>
                        {
                            { "type", req.GetType().Name },
                            { "success", ok },
                            { "failed", failed },
                            { "progress", req.GetProgress(ok) ?? string.Empty }
                        });
                    }
                }
                imperatives.Add(new Dictionary<string, object>
                {
                    { "id", achievement.Id },
                    { "name", achievement.Name },
                    { "achieved", achieved },
                    { "failed", status != null && status.failed },
                    { "requirements", requirements }
                });
            }
            return imperatives;
        }

        private static List<string> ReadUnlockedAchievements()
        {
            List<string> unlocked = new List<string>();
            ColonyAchievementTracker tracker = SaveGame.Instance?.ColonyAchievementTracker;
            if (tracker?.achievements == null)
            {
                return unlocked;
            }
            foreach (KeyValuePair<string, ColonyAchievementStatus> pair in tracker.achievements)
            {
                if (pair.Value != null && pair.Value.success && !pair.Value.failed)
                {
                    unlocked.Add(pair.Key);
                }
            }
            unlocked.Sort(StringComparer.Ordinal);
            return unlocked;
        }
    }
}
