using System.Collections.Generic;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// What cooking stations did to the colony's food, per daily report.  The
    /// game's daily food report counts harvests as made and meals as used, but a
    /// craft is invisible to it: the dish is never counted as made and the edible
    /// ingredients are never counted as used.  Per craft CookedFoodPatch records
    ///   gross:    the dish's calories,
    ///   inputs:   the edible calories its ingredients took from the station,
    ///   reported: what the game's report changed by during the craft (normally 0).
    /// gross - inputs is the food cooking really added; a Mush Bar from dirt adds
    /// all of it, a Pickled Meal from Mealwood adds nothing.
    ///
    /// Days are ReportManager.DailyReport.day (the game opens a report at
    /// nightfall), so the numbers share the native report's window.  The ledger
    /// covers the loaded game only: it starts empty at every load.
    /// </summary>
    internal static class ApiFoodLedger
    {
        private sealed class Day
        {
            internal double Gross;
            internal double Inputs;
            internal double Reported;
        }

        private static readonly Dictionary<int, Day> Days = new Dictionary<int, Day>();
        private static object game;
        private static int firstFullDay = int.MaxValue;
        private static double countingSince = double.NaN;

        internal static void Add(object currentGame, int day, double timeInCycles, double gross, double inputs, double reported)
        {
            Follow(currentGame, day, timeInCycles);
            if (!Finite(gross) || !Finite(inputs) || !Finite(reported))
            {
                return;
            }
            if (!Days.TryGetValue(day, out Day entry))
            {
                entry = new Day();
                Days[day] = entry;
            }
            entry.Gross += gross;
            entry.Inputs += inputs;
            entry.Reported += reported;
        }

        /// <summary>
        /// The cooking fields for today's and yesterday's reports.  todayNative
        /// and yesterdayNative are those reports' own net, in calories.  The
        /// yesterday_* fields are null unless the whole of that report was counted.
        /// </summary>
        internal static Dictionary<string, object> Read(
            object currentGame, int today, double todayNative, int? yesterday, double? yesterdayNative, double timeInCycles)
        {
            Follow(currentGame, today, timeInCycles);
            Dictionary<string, object> fields = new Dictionary<string, object>();
            Fill(fields, "today", today, todayNative);
            if (yesterday.HasValue && yesterday.Value >= firstFullDay && yesterdayNative.HasValue)
            {
                Fill(fields, "yesterday", yesterday.Value, yesterdayNative.Value);
            }
            else
            {
                foreach (string name in new[] { "cooked", "cooked_inputs", "cooked_net", "cooked_reported", "net_with_cooking" })
                {
                    fields["yesterday_" + name] = null;
                }
            }
            fields["cooked_counted_since_cycle"] = System.Math.Round(countingSince + 1.0, 2);
            return fields;
        }

        private static void Fill(Dictionary<string, object> fields, string prefix, int day, double native)
        {
            Days.TryGetValue(day, out Day entry);
            double gross = entry == null ? 0.0 : entry.Gross;
            double inputs = entry == null ? 0.0 : entry.Inputs;
            double reported = entry == null ? 0.0 : entry.Reported;
            double cookedNet = gross - inputs;
            fields[prefix + "_cooked"] = Kcal(gross);
            fields[prefix + "_cooked_inputs"] = Kcal(inputs);
            fields[prefix + "_cooked_net"] = Kcal(cookedNet);
            fields[prefix + "_cooked_reported"] = Kcal(reported);
            // The report's net, minus what it already said about crafts, plus
            // what crafts really did.
            fields[prefix + "_net_with_cooking"] = Kcal(native - reported + cookedNet);
        }

        // A new game object means a load: the ledger restarts.  So does a report
        // day that went backwards.  The report open at that moment began before
        // counting did, so the first whole report is the next one.
        private static void Follow(object currentGame, int day, double timeInCycles)
        {
            bool reloaded = !ReferenceEquals(currentGame, game);
            bool rewound = !reloaded && Days.Count > 0 && day < MaxDay();
            if (!reloaded && !rewound)
            {
                return;
            }
            game = currentGame;
            Days.Clear();
            countingSince = timeInCycles;
            firstFullDay = day + 1;
        }

        private static int MaxDay()
        {
            int max = int.MinValue;
            foreach (int key in Days.Keys)
            {
                if (key > max)
                {
                    max = key;
                }
            }
            return max;
        }

        private static double Kcal(double calories)
        {
            return System.Math.Round(calories / 1000.0, 0);
        }

        private static bool Finite(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }
    }
}
