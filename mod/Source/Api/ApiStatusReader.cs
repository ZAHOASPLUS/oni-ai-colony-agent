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
        // The game counts food in calories; players read kcal.
        private const float CaloriesPerKcal = 1000f;
        // The game's energy report counts joules.
        private const float JoulesPerKj = 1000f;
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
                { "work", ReadWork(world.id) },
                { "printables_waiting", Immigration.Instance.ImmigrantsAvailable },
                { "schedules", ReadSchedules() }
            };
        }

        private static object ReadSchedules()
        {
            return ScheduleManager.Instance.GetSchedules().Select(schedule => (object)new Dictionary<string, object>
            {
                { "blocks", ApiPlayTools.Describe(schedule) },
                { "current_block", schedule.GetCurrentBlockIdx() }
            }).ToList();
        }

        /// <summary>One row of get_progress: the numbers the stage goal is judged on.</summary>
        internal static Dictionary<string, object> ReadProgressRow()
        {
            WorldContainer world = ClusterManager.Instance.activeWorld;
            int duplicants = Components.LiveMinionIdentities.Items.Count(identity => identity != null);
            double foodKcal = ReadFoodKcal(world);
            return new Dictionary<string, object>
            {
                { "cycle", GameClock.Instance.GetCycle() + 1 },
                { "duplicants", duplicants },
                { "food_kcal", System.Math.Round(foodKcal, 0) },
                { "food_days", duplicants == 0 ? 0.0 : System.Math.Round(foodKcal / (KcalPerDuplicantCycle * duplicants), 1) },
                { "breathable_kg", ApiValues.Number(ReadBreathableKg(world), 0) },
                { "oxygen_report_kg", ReadReport(ReportManager.ReportType.OxygenCreated) },
                { "power", ReadPower() },
                { "alerts", ReadAlerts().Count(alert => alert.Severity != "minor") }
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
                entry["id"] = ApiDuplicants.Id(identity);
                entry["name"] = identity.GetProperName();
                entry["world"] = identity.GetMyWorldId();
                entry["chore"] = chore?.choreType?.Id;
                entry["stress_pct"] = AmountValue(amounts.Stress, go);
                entry["stress_causes"] = StressCauses(amounts.Stress, go);
                entry["morale"] = AttributeValue(Db.Get().Attributes.QualityOfLife, go);
                entry["morale_expected"] = AttributeValue(Db.Get().Attributes.QualityOfLifeExpectation, go);
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

        /// <summary>What moves stress right now, in percent per cycle.</summary>
        private static List<object> StressCauses(Amount stress, GameObject go)
        {
            List<object> causes = new List<object>();
            AmountInstance instance = stress.Lookup(go);
            if (instance == null)
            {
                return causes;
            }
            ArrayRef<AttributeModifier> modifiers = instance.deltaAttribute.Modifiers;
            for (int i = 0; i != modifiers.Count; i++)
            {
                AttributeModifier modifier = modifiers[i];
                if (modifier.Value != 0f)
                {
                    causes.Add(new Dictionary<string, object>
                    {
                        ["what"] = modifier.GetDescription(),
                        ["per_cycle"] = System.Math.Round(modifier.Value * 600f, 1),
                    });
                }
            }
            return causes;
        }

        private static object AttributeValue(Klei.AI.Attribute attribute, GameObject go)
        {
            AttributeInstance instance = attribute.Lookup(go);
            return instance == null ? null : (object)ApiValues.Number(instance.GetTotalValue(), 1);
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

        private const int UnreachableLimit = 12;

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
                names.Add(STRINGS.UI.StripLinkFormatting(items.Current.GetName()));
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
                { "food_report_kcal", ReadFoodReport() },
                { "breathable_kg", ApiValues.Number(ReadBreathableKg(world), 0) },
                { "oxygen_report_kg", ReadReport(ReportManager.ReportType.OxygenCreated) },
                { "power_report_kj", ReadReport(ReportManager.ReportType.EnergyCreated, JoulesPerKj) },
                { "power", ReadPower() }
            };
        }

        private static double ReadFoodKcal(WorldContainer world)
        {
            HashSet<GameObject> seen = new HashSet<GameObject>();
            double total = 0.0;
            ICollection<Pickupable> food = world.worldInventory.GetPickupables(GameTags.Edible);
            if (food != null)
            {
                foreach (Pickupable pickupable in food)
                {
                    AddFood(pickupable == null ? null : pickupable.gameObject, seen, ref total);
                }
            }

            // WorldInventory does not enumerate food held in private Storage
            // components such as a RationBox. Include those items explicitly.
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
                        AddFood(item, seen, ref total);
                    }
                }
            }
            return total;
        }

        private static void AddFood(GameObject item, HashSet<GameObject> seen, ref double total)
        {
            if (item == null || !seen.Add(item))
            {
                return;
            }
            Edible edible = item.GetComponent<Edible>();
            if (edible != null && edible.Calories > 0f &&
                !float.IsNaN(edible.Calories) && !float.IsInfinity(edible.Calories))
            {
                // Edible.Calories is in calories; players read kcal.
                total += edible.Calories / 1000.0;
            }
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

        // The game's food report counts harvests and butchering as made, and meals
        // and the edible ingredients cooked as used, but never the dish a cooking
        // station makes.  today_cooked and yesterday_cooked come from
        // ApiFoodLedger, which counts those dishes since the save was loaded.
        private static object ReadFoodReport()
        {
            Dictionary<string, object> report =
                ReadReport(ReportManager.ReportType.CaloriesCreated, CaloriesPerKcal) as Dictionary<string, object>;
            GameClock clock = GameClock.Instance;
            ReportManager reports = ReportManager.Instance;
            if (clock == null || report == null || ReferenceEquals(reports, null))
            {
                return report;
            }
            ReportManager.DailyReport earlier = reports.YesterdaysReport;
            int? yesterday = earlier == null ? (int?)null : earlier.day;
            foreach (KeyValuePair<string, object> entry in
                     ApiFoodLedger.Read(Game.Instance, reports.TodaysReport.day, yesterday, clock.GetTimeInCycles()))
            {
                report[entry.Key] = entry.Value;
            }
            return report;
        }

        // Today's positive and negative totals and yesterday's net, as in the
        // game's daily report, each divided by unit.  The report is colony-wide.
        // The game opens a new report at nightfall (ReportManager.OnNightTime),
        // not at the cycle boundary; report_day is the open report's own number.
        internal static object ReadReport(ReportManager.ReportType type, float unit = 1f)
        {
            // Managed null check: ReportManager clears Instance in OnCleanUp.
            ReportManager reports = ReportManager.Instance;
            if (ReferenceEquals(reports, null) || reports.TodaysReport == null)
            {
                return null;
            }
            ReportManager.ReportEntry today = reports.TodaysReport.GetEntry(type);
            ReportManager.DailyReport earlier = reports.YesterdaysReport;
            return new Dictionary<string, object>
            {
                { "report_day", reports.TodaysReport.day },
                { "today_made", ApiValues.Number(today.Positive / unit, 0) },
                { "today_used", ApiValues.Number(today.Negative / unit, 0) },
                { "yesterday_net", earlier == null ? null : ApiValues.Number(earlier.GetEntry(type).Net / unit, 0) }
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
            Database.BuildingStatusItems statusItems = Db.Get().BuildingStatusItems;
            List<object> unreachable = new List<object>();
            int blueprints = 0;
            foreach (Prioritizable item in Components.Prioritizables.Items)
            {
                if (item != null && item.GetMyWorldId() == worldId && item.GetComponent<Constructable>() != null)
                {
                    blueprints++;
                    KSelectable selectable = item.GetComponent<KSelectable>();
                    if (selectable.HasStatusItem(statusItems.ConstructionUnreachable) ||
                        selectable.HasStatusItem(statusItems.ConstructableDigUnreachable))
                    {
                        NoteUnreachable(unreachable, item);
                    }
                }
            }
            int digs = 0;
            foreach (Diggable diggable in Components.Diggables.Items)
            {
                if (diggable != null && diggable.GetMyWorldId() == worldId)
                {
                    digs++;
                    if (diggable.GetComponent<KSelectable>().HasStatusItem(statusItems.DigUnreachable))
                    {
                        NoteUnreachable(unreachable, diggable);
                    }
                }
            }
            return new Dictionary<string, object>
            {
                { "blueprints", blueprints },
                { "dig_marks", digs },
                { "unreachable", unreachable }
            };
        }

        private static void NoteUnreachable(List<object> unreachable, KMonoBehaviour target)
        {
            if (unreachable.Count < UnreachableLimit)
            {
                Dictionary<string, object> entry = ApiValues.Xy(Grid.PosToCell(target));
                entry["what"] = target.PrefabID().Name;
                unreachable.Add(entry);
            }
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
