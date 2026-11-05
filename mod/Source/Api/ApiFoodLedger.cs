using System.Collections.Generic;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Calories of the dishes cooking stations make, per daily report.  The game's
    /// daily food report counts harvests as made and the edible ingredients cooked
    /// as used, but never the dish itself, so a colony fed by a Microbe Musher
    /// reads as producing nothing.  The ledger fills that gap for the loaded game
    /// only: it starts empty at every load and is not saved.
    ///
    /// Days are ReportManager.DailyReport.day, not GameClock cycles: the game
    /// starts a new report at nightfall, so keying by report keeps today_cooked in
    /// the same window as today_made and today_used.
    /// </summary>
    internal static class ApiFoodLedger
    {
        private static readonly Dictionary<int, double> CaloriesByDay = new Dictionary<int, double>();
        private static object game;
        private static int firstFullDay = int.MaxValue;
        private static double countingSince = double.NaN;

        /// <summary>Record one finished dish into the report of the given day.</summary>
        internal static void Add(object currentGame, int day, double timeInCycles, double calories)
        {
            Follow(currentGame, day, timeInCycles);
            if (calories <= 0.0 || double.IsNaN(calories) || double.IsInfinity(calories))
            {
                return;
            }
            CaloriesByDay.TryGetValue(day, out double total);
            CaloriesByDay[day] = total + calories;
        }

        /// <summary>
        /// today_cooked and yesterday_cooked in kcal for the reports of today and
        /// yesterday.  yesterday_cooked is null unless the whole of yesterday's
        /// report was counted; cooked_counted_since_cycle says when counting began.
        /// </summary>
        internal static Dictionary<string, object> Read(object currentGame, int today, int? yesterday, double timeInCycles)
        {
            Follow(currentGame, today, timeInCycles);
            CaloriesByDay.TryGetValue(today, out double cooked);
            object earlierCooked = null;
            if (yesterday.HasValue && yesterday.Value >= firstFullDay)
            {
                CaloriesByDay.TryGetValue(yesterday.Value, out double earlier);
                earlierCooked = System.Math.Round(earlier / 1000.0, 0);
            }
            return new Dictionary<string, object>
            {
                { "today_cooked", System.Math.Round(cooked / 1000.0, 0) },
                { "yesterday_cooked", earlierCooked },
                { "cooked_counted_since_cycle", System.Math.Round(countingSince + 1.0, 2) }
            };
        }

        // A new game object means a load: the ledger restarts.  So does a report
        // day that went backwards.  The report open at that moment began before
        // counting did, so the first whole report is the next one.
        private static void Follow(object currentGame, int day, double timeInCycles)
        {
            bool reloaded = !ReferenceEquals(currentGame, game);
            bool rewound = !reloaded && CaloriesByDay.Count > 0 && day < MaxDay();
            if (!reloaded && !rewound)
            {
                return;
            }
            game = currentGame;
            CaloriesByDay.Clear();
            countingSince = timeInCycles;
            firstFullDay = day + 1;
        }

        private static int MaxDay()
        {
            int max = int.MinValue;
            foreach (int key in CaloriesByDay.Keys)
            {
                if (key > max)
                {
                    max = key;
                }
            }
            return max;
        }
    }
}
