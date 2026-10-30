using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Klei.AI;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>The colony at a glance: everything a player reads off the HUD.</summary>
    internal static class ApiStatusReader
    {
        // One duplicant eats 1000 kcal per cycle (DUPLICANTSTATS.STANDARD).
        private const double KcalPerDuplicantCycle = 1000.0;
        private const int StatusItemsPerDuplicant = 6;

        private static readonly FieldInfo NotificationsField =
            AccessTools.Field(typeof(NotificationManager), "notifications");

        internal sealed class Alert
        {
            internal string Title;
            internal string Severity;
            internal int Count;
        }

        internal static object Read(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            List<object> duplicants = ReadDuplicants();
            return new Dictionary<string, object>
            {
                { "time", ReadTime() },
                { "worlds", ReadWorlds() },
                { "duplicants", duplicants },
                { "colony", ReadColony(world, duplicants.Count) },
                { "alerts", ReadAlerts().Select(alert => (object)new Dictionary<string, object>
                    {
                        { "title", alert.Title },
                        { "severity", alert.Severity },
                        { "count", alert.Count }
                    }).ToList() },
                { "research", ReadResearch() },
                { "work", ReadWork(world.id) }
            };
        }

        private static object ReadTime()
        {
            SpeedControlScreen speed = SpeedControlScreen.Instance;
            return new Dictionary<string, object>
            {
                { "cycle", GameClock.Instance.GetCycle() + 1 },
                { "cycle_fraction", ApiValues.Number(GameClock.Instance.GetCurrentCycleAsPercentage(), 3) },
                { "is_night", GameClock.Instance.IsNighttime() },
                { "paused", speed.IsPaused },
                // Same numbering as the game's three speed buttons.
                { "speed", speed.GetSpeed() + 1 }
            };
        }

        private static object ReadWorlds()
        {
            List<object> worlds = new List<object>();
            foreach (WorldContainer world in ClusterManager.Instance.WorldContainers)
            {
                if (!world.IsDiscovered)
                {
                    continue;
                }
                worlds.Add(new Dictionary<string, object>
                {
                    { "id", world.id },
                    { "name", world.GetProperName() },
                    { "active", world.id == ClusterManager.Instance.activeWorldId },
                    { "x_min", (int)world.minimumBounds.x },
                    { "y_min", (int)world.minimumBounds.y },
                    { "x_max", (int)world.maximumBounds.x },
                    { "y_max", (int)world.maximumBounds.y },
                    { "rocket_interior", world.IsModuleInterior }
                });
            }
            return worlds;
        }

        private static List<object> ReadDuplicants()
        {
            Database.Amounts amounts = Db.Get().Amounts;
            List<object> duplicants = new List<object>();
            foreach (MinionIdentity identity in Components.LiveMinionIdentities.Items)
            {
                if (identity == null)
                {
                    continue;
                }
                GameObject go = identity.gameObject;
                int cell = Grid.PosToCell(go);
                ChoreDriver driver = go.GetComponent<ChoreDriver>();
                Chore chore = driver == null ? null : driver.GetCurrentChore();
                Dictionary<string, object> entry = ApiValues.Xy(cell);
                entry["name"] = identity.GetProperName();
                entry["world"] = identity.GetMyWorldId();
                entry["chore"] = chore?.choreType?.Id;
                entry["stress_pct"] = AmountValue(amounts.Stress, go);
                entry["health"] = AmountValue(amounts.HitPoints, go);
                entry["breath_pct"] = AmountValue(amounts.Breath, go);
                entry["kcal"] = Kcal(amounts.Calories, go);
                entry["body_c"] = AmountValue(amounts.Temperature, go, kelvin: true);
                entry["air"] = DescribeAir(Grid.CellAbove(cell));
                entry["status"] = ReadStatusItems(go, StatusItemsPerDuplicant);
                duplicants.Add(entry);
            }
            return duplicants;
        }

        private static object AmountValue(Amount amount, GameObject go, bool kelvin = false)
        {
            AmountInstance instance = amount.Lookup(go);
            if (instance == null)
            {
                return null;
            }
            return kelvin ? ApiValues.Celsius(instance.value) : ApiValues.Number(instance.value, 1);
        }

        private static object Kcal(Amount amount, GameObject go)
        {
            AmountInstance instance = amount.Lookup(go);
            return instance == null ? null : ApiValues.Number(instance.value / 1000f, 0);
        }

        private static string DescribeAir(int cell)
        {
            if (!Grid.IsValidCell(cell))
            {
                return null;
            }
            Element element = Grid.Element[cell];
            return element.IsSolid
                ? "solid"
                : element.id + " " + System.Math.Round(Grid.Mass[cell], 2) + " kg";
        }

        internal static List<object> ReadStatusItems(GameObject go, int limit)
        {
            List<object> names = new List<object>();
            KSelectable selectable = go.GetComponent<KSelectable>();
            StatusItemGroup group = selectable == null ? null : selectable.GetStatusItemGroup();
            if (group == null)
            {
                return names;
            }
            IEnumerator<StatusItemGroup.Entry> items = group.GetEnumerator();
            while (items.MoveNext() && names.Count < limit)
            {
                names.Add(items.Current.GetName());
            }
            return names;
        }

        private static object ReadColony(WorldContainer world, int duplicantCount)
        {
            double foodKcal = ReadFoodKcal(world);
            return new Dictionary<string, object>
            {
                { "food_kcal", System.Math.Round(foodKcal, 0) },
                { "food_days", duplicantCount == 0 ? 0.0 : System.Math.Round(foodKcal / (KcalPerDuplicantCycle * duplicantCount), 1) },
                { "breathable_kg", ApiValues.Number(ReadBreathableKg(world), 0) },
                { "oxygen_report_kg", ReadReport(ReportManager.ReportType.OxygenCreated) },
                { "power_report_kj", ReadReport(ReportManager.ReportType.EnergyCreated) },
                { "power", ReadPower() }
            };
        }

        private static double ReadFoodKcal(WorldContainer world)
        {
            ICollection<Pickupable> food = world.worldInventory.GetPickupables(GameTags.Edible);
            if (food == null)
            {
                return 0.0;
            }
            double total = 0.0;
            foreach (Pickupable pickupable in food)
            {
                if (pickupable == null || pickupable.KPrefabID.HasTag(GameTags.StoredPrivate))
                {
                    continue;
                }
                Edible edible = pickupable.GetComponent<Edible>();
                if (edible != null && edible.Calories > 0f && !float.IsInfinity(edible.Calories))
                {
                    // Edible.Calories is in calories; players read kcal.
                    total += edible.Calories / 1000.0;
                }
            }
            return total;
        }

        private static float ReadBreathableKg(WorldContainer world)
        {
            float total = 0f;
            int xMin = (int)world.minimumBounds.x;
            int xMax = (int)world.maximumBounds.x;
            int yMin = (int)world.minimumBounds.y;
            int yMax = (int)world.maximumBounds.y;
            for (int y = yMin; y <= yMax; y++)
            {
                for (int x = xMin; x <= xMax; x++)
                {
                    int cell = Grid.XYToCell(x, y);
                    SimHashes id = Grid.Element[cell].id;
                    if (id == SimHashes.Oxygen || id == SimHashes.ContaminatedOxygen)
                    {
                        total += Grid.Mass[cell];
                    }
                }
            }
            return total;
        }

        // Today's positive and negative totals and yesterday's net, as in the
        // game's daily report.  The report is colony-wide.
        private static object ReadReport(ReportManager.ReportType type)
        {
            ReportManager reports = ReportManager.Instance;
            if (reports == null || reports.TodaysReport == null)
            {
                return null;
            }
            ReportManager.ReportEntry today = reports.TodaysReport.GetEntry(type);
            ReportManager.DailyReport earlier = reports.YesterdaysReport;
            return new Dictionary<string, object>
            {
                { "today_made", ApiValues.Number(today.Positive, 0) },
                { "today_used", ApiValues.Number(today.Negative, 0) },
                { "yesterday_net", earlier == null ? null : ApiValues.Number(earlier.GetEntry(type).Net, 0) }
            };
        }

        private static object ReadPower()
        {
            CircuitManager circuits = Game.Instance.circuitManager;
            HashSet<ushort> ids = new HashSet<ushort>();
            float capacity = 0f;
            float stored = 0f;
            foreach (Generator generator in Components.Generators.Items)
            {
                ids.Add(circuits.GetCircuitID(generator));
            }
            foreach (EnergyConsumer consumer in Components.EnergyConsumers.Items)
            {
                ids.Add(circuits.GetCircuitID(consumer));
            }
            foreach (Battery battery in Components.Batteries.Items)
            {
                ids.Add(circuits.GetCircuitID(battery));
                capacity += battery.Capacity;
                stored += battery.JoulesAvailable;
            }
            ids.Remove(CircuitManager.INVALID_ID);

            float generated = 0f;
            float used = 0f;
            float needed = 0f;
            foreach (ushort id in ids)
            {
                generated += circuits.GetWattsGeneratedByCircuit(id);
                used += circuits.GetWattsUsedByCircuit(id);
                needed += circuits.GetWattsNeededWhenActive(id);
            }
            return new Dictionary<string, object>
            {
                { "circuits", ids.Count },
                { "generated_w", ApiValues.Number(generated, 0) },
                { "used_w", ApiValues.Number(used, 0) },
                { "needed_when_active_w", ApiValues.Number(needed, 0) },
                { "battery_stored_kj", ApiValues.Number(stored / 1000f, 0) },
                { "battery_capacity_kj", ApiValues.Number(capacity / 1000f, 0) }
            };
        }

        private static object ReadResearch()
        {
            TechInstance active = Research.Instance == null ? null : Research.Instance.GetActiveResearch();
            if (active?.tech == null)
            {
                return new Dictionary<string, object> { { "current", null } };
            }
            return new Dictionary<string, object>
            {
                { "current", active.tech.Id },
                { "progress", ApiValues.Number(active.GetTotalPercentageComplete(), 3) }
            };
        }

        private static object ReadWork(int worldId)
        {
            int blueprints = 0;
            foreach (Prioritizable item in Components.Prioritizables.Items)
            {
                if (item != null && item.GetMyWorldId() == worldId && item.GetComponent<Constructable>() != null)
                {
                    blueprints++;
                }
            }
            int digs = 0;
            foreach (Diggable diggable in Components.Diggables.Items)
            {
                if (diggable != null && diggable.GetMyWorldId() == worldId)
                {
                    digs++;
                }
            }
            return new Dictionary<string, object>
            {
                { "blueprints", blueprints },
                { "dig_marks", digs }
            };
        }

        /// <summary>The problems the game itself is showing the player, grouped by title.</summary>
        internal static List<Alert> ReadAlerts()
        {
            List<Alert> alerts = new List<Alert>();
            NotificationManager manager = NotificationManager.Instance;
            if (manager == null)
            {
                return alerts;
            }
            foreach (Notification notification in (List<Notification>)NotificationsField.GetValue(manager))
            {
                string severity;
                switch (notification.Type)
                {
                    case NotificationType.DuplicantThreatening:
                        severity = "critical";
                        break;
                    case NotificationType.Bad:
                        severity = "bad";
                        break;
                    case NotificationType.BadMinor:
                        severity = "minor";
                        break;
                    default:
                        continue;
                }
                string title = notification.titleText ?? string.Empty;
                Alert existing = alerts.Find(alert => alert.Title == title);
                if (existing != null)
                {
                    existing.Count++;
                    continue;
                }
                alerts.Add(new Alert { Title = title, Severity = severity, Count = 1 });
            }
            return alerts;
        }
    }
}
