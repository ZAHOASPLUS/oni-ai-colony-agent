using System.Collections.Generic;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Calories of the dishes cooking stations make, per cycle.  The game's daily
    /// food report counts harvests as made and the edible ingredients cooked as
    /// used, but never the dish itself, so a colony fed by a Microbe Musher reads
    /// as producing nothing.  The ledger fills that gap for the loaded game only:
    /// it starts empty at every load and is not saved.
    /// </summary>
    internal static class ApiFoodLedger
    {
        private static readonly Dictionary<int, double> CaloriesByCycle = new Dictionary<int, double>();
        private static object game;
        private static int firstFullCycle = int.MaxValue;
        private static double countingSince = double.NaN;

        /// <summary>Record one finished dish; cycle is GameClock.GetCycle().</summary>
        internal static void Add(object currentGame, int cycle, double timeInCycles, double calories)
        {
            Follow(currentGame, cycle, timeInCycles);
            if (calories <= 0.0 || double.IsNaN(calories) || double.IsInfinity(calories))
            {
                return;
            }
            CaloriesByCycle.TryGetValue(cycle, out double total);
            CaloriesByCycle[cycle] = total + calories;
        }

        /// <summary>
        /// today_cooked and yesterday_cooked in kcal.  yesterday_cooked is null when
        /// counting began after yesterday started, so a partial count never reads
        /// as a whole cycle; counted_since says when counting began.
        /// </summary>
        internal static Dictionary<string, object> Read(object currentGame, int cycle, double timeInCycles)
        {
            Follow(currentGame, cycle, timeInCycles);
            CaloriesByCycle.TryGetValue(cycle, out double today);
            object yesterday = null;
            if (cycle - 1 >= firstFullCycle)
            {
                CaloriesByCycle.TryGetValue(cycle - 1, out double earlier);
                yesterday = System.Math.Round(earlier / 1000.0, 0);
            }
            return new Dictionary<string, object>
            {
                { "today_cooked", System.Math.Round(today / 1000.0, 0) },
                { "yesterday_cooked", yesterday },
                { "cooked_counted_since_cycle", System.Math.Round(countingSince + 1.0, 2) }
            };
        }

        // A new game object means a load: the ledger restarts.  So does a clock
        // that went backwards.
        private static void Follow(object currentGame, int cycle, double timeInCycles)
        {
            bool reloaded = !ReferenceEquals(currentGame, game);
            bool rewound = !reloaded && CaloriesByCycle.Count > 0 && cycle < MaxCycle();
            if (!reloaded && !rewound)
            {
                return;
            }
            game = currentGame;
            CaloriesByCycle.Clear();
            countingSince = timeInCycles;
            // Counting from the very start of a cycle covers all of it.
            firstFullCycle = timeInCycles - cycle < 1e-6 ? cycle : cycle + 1;
        }

        private static int MaxCycle()
        {
            int max = int.MinValue;
            foreach (int key in CaloriesByCycle.Keys)
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
