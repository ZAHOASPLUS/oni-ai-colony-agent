using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// What changed since the caller last looked.  The game state is compared
    /// about once a second instead of hooking each game event, so a change the
    /// game reports in some unexpected way still shows up.  A ring buffer with
    /// sequence numbers: a caller that fell behind is told how much it missed.
    /// </summary>
    internal static class ApiEvents
    {
        private const int Capacity = 2000;
        private const int DefaultLimit = 100;
        private const float TickSeconds = 1f;
        private const float LowBreathPercent = 60f;

        private static readonly Queue<Dictionary<string, object>> Buffer = new Queue<Dictionary<string, object>>();
        private static long nextSequence = 1;
        private static float nextTickAt;
        private static Baseline previous;
        // Buildings the player ordered.  Map features spawn as an area is revealed
        // and must not read as finished work.
        private static readonly HashSet<string> Ordered = new HashSet<string>(StringComparer.Ordinal);

        internal static List<Dictionary<string, object>> CycleLog { get; } = new List<Dictionary<string, object>>();

        internal static int DuplicantsLost { get; private set; }

        private sealed class Baseline
        {
            internal int Cycle;
            internal Dictionary<string, string> Alerts = new Dictionary<string, string>(StringComparer.Ordinal);
            internal Dictionary<int, string> Buildings = new Dictionary<int, string>();
            internal Dictionary<int, int> BuildingCells = new Dictionary<int, int>();
            internal Dictionary<int, string> Duplicants = new Dictionary<int, string>();
            internal HashSet<string> Techs = new HashSet<string>(StringComparer.Ordinal);
            // Duplicant ids: two duplicants can share a name.
            internal HashSet<int> LowBreath = new HashSet<int>();
        }

        internal static void ExpectBuilding(string def, int cell)
        {
            Ordered.Add(OrderKey(def, cell));
        }

        private static string OrderKey(string def, int cell)
        {
            return def + "@" + cell;
        }

        internal static void Add(string type, Dictionary<string, object> fields = null)
        {
            Dictionary<string, object> entry = new Dictionary<string, object>
            {
                { "seq", nextSequence++ },
                { "type", type }
            };
            if (GameClock.Instance != null)
            {
                entry["cycle"] = Math.Round(GameClock.Instance.GetTimeInCycles() + 1.0, 2);
            }
            if (fields != null)
            {
                foreach (KeyValuePair<string, object> field in fields)
                {
                    entry[field.Key] = field.Value;
                }
            }
            Buffer.Enqueue(entry);
            while (Buffer.Count > Capacity)
            {
                Buffer.Dequeue();
            }
        }

        internal static void Tick(float unscaledTime)
        {
            if (unscaledTime < nextTickAt)
            {
                return;
            }
            nextTickAt = unscaledTime + TickSeconds;

            if (ApiLifecycle.State() != ApiLifecycle.InGame)
            {
                previous = null;
                return;
            }

            try
            {
                Baseline current = Observe();
                if (current.Duplicants.Count == 0 || current.Buildings.Count == 0)
                {
                    return;  // The world is still loading; this is no baseline.
                }
                if (previous == null)
                {
                    Add("session_started", new Dictionary<string, object> { { "save", SaveLoader.GetActiveSaveFilePath() } });
                }
                else
                {
                    Compare(previous, current);
                }
                previous = current;
            }
            catch (Exception ex)
            {
                // Events are a convenience; get_status still tells the truth.
                ApiLog.Error("api_events", ex);
            }
        }

        internal static object Read(JObject args)
        {
            long since = ApiArgs.Int(args, "since", 0);
            int limit = Math.Max(1, ApiArgs.Int(args, "limit", DefaultLimit));
            List<Dictionary<string, object>> matching = Buffer
                .Where(entry => (long)entry["seq"] > since)
                .ToList();
            long oldest = Buffer.Count == 0 ? nextSequence : (long)Buffer.Peek()["seq"];
            List<Dictionary<string, object>> page = matching.Take(limit).ToList();
            return new Dictionary<string, object>
            {
                { "events", page },
                { "next_since", page.Count == 0 ? since : (long)page[page.Count - 1]["seq"] },
                { "more", matching.Count > page.Count },
                { "missed", Math.Max(0, oldest - since - 1) }
            };
        }

        private static Baseline Observe()
        {
            Baseline state = new Baseline
            {
                Cycle = GameClock.Instance.GetCycle() + 1
            };
            foreach (ApiStatusReader.Alert alert in ApiStatusReader.ReadAlerts())
            {
                state.Alerts[alert.Title] = alert.Severity;
            }
            foreach (BuildingComplete building in Components.BuildingCompletes.Items)
            {
                if (building != null)
                {
                    KPrefabID prefab = building.GetComponent<KPrefabID>();
                    state.Buildings[prefab.InstanceID] = prefab.PrefabTag.Name;
                    state.BuildingCells[prefab.InstanceID] = Grid.PosToCell(building);
                }
            }
            foreach (MinionIdentity identity in Components.LiveMinionIdentities.Items)
            {
                if (identity != null)
                {
                    int id = ApiDuplicants.Id(identity);
                    state.Duplicants[id] = identity.GetProperName();
                    if (Db.Get().Amounts.Breath.Lookup(identity.gameObject).value < LowBreathPercent)
                    {
                        state.LowBreath.Add(id);
                    }
                }
            }
            foreach (Tech tech in Db.Get().Techs.resources)
            {
                if (tech.IsComplete())
                {
                    state.Techs.Add(tech.Id);
                }
            }
            return state;
        }

        private static void Compare(Baseline before, Baseline after)
        {
            if (after.Cycle != before.Cycle)
            {
                Add("cycle_started", new Dictionary<string, object> { { "number", after.Cycle } });
                CycleLog.Add(ApiStatusReader.ReadProgressRow());
            }
            foreach (KeyValuePair<string, string> alert in after.Alerts)
            {
                if (!before.Alerts.ContainsKey(alert.Key))
                {
                    Add("alert_raised", new Dictionary<string, object>
                    {
                        { "title", alert.Key },
                        { "severity", alert.Value }
                    });
                }
            }
            foreach (string title in before.Alerts.Keys.Where(key => !after.Alerts.ContainsKey(key)))
            {
                Add("alert_cleared", new Dictionary<string, object> { { "title", title } });
            }
            foreach (KeyValuePair<int, string> building in after.Buildings)
            {
                if (!before.Buildings.ContainsKey(building.Key) &&
                    Ordered.Remove(OrderKey(building.Value, after.BuildingCells[building.Key])))
                {
                    Add("building_completed", new Dictionary<string, object>
                    {
                        { "id", building.Key },
                        { "def", building.Value }
                    });
                }
            }
            foreach (KeyValuePair<int, string> building in before.Buildings)
            {
                if (!after.Buildings.ContainsKey(building.Key))
                {
                    Add("building_removed", new Dictionary<string, object>
                    {
                        { "id", building.Key },
                        { "def", building.Value }
                    });
                }
            }
            foreach (KeyValuePair<int, string> duplicant in before.Duplicants)
            {
                if (!after.Duplicants.ContainsKey(duplicant.Key))
                {
                    Add("duplicant_lost", new Dictionary<string, object> { { "id", duplicant.Key }, { "name", duplicant.Value } });
                    DuplicantsLost++;
                }
            }
            foreach (KeyValuePair<int, string> duplicant in after.Duplicants)
            {
                if (!before.Duplicants.ContainsKey(duplicant.Key))
                {
                    Add("duplicant_arrived", new Dictionary<string, object> { { "id", duplicant.Key }, { "name", duplicant.Value } });
                }
            }
            foreach (int id in after.LowBreath.Where(duplicant => !before.LowBreath.Contains(duplicant)))
            {
                Add("duplicant_low_breath", new Dictionary<string, object> { { "id", id }, { "name", after.Duplicants[id] } });
            }
            foreach (string tech in after.Techs.Where(id => !before.Techs.Contains(id)))
            {
                Add("research_completed", new Dictionary<string, object> { { "tech", tech } });
            }
        }
    }
}
