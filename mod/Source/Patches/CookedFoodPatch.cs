using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using OniAiProbeMod.Api;
using UnityEngine;

namespace OniAiProbeMod.Patches
{
    /// <summary>
    /// Every dish a cooking station finishes goes through
    /// ComplexFabricator.SpawnOrderProduct or one of its overrides (Microbe
    /// Musher, Electric Grill, Gas Range, Deep Fryer, Sushi Bar ...).  Overrides
    /// call down to the base, so only the outermost call on the stack counts.
    ///
    /// The game's daily food report counts harvests as made and meals as used.
    /// SpawnOrderProduct consumes the ingredients through
    /// Storage.ConsumeAndGetDisease, which reports nothing, and its one
    /// ReportValue looks for food tagged like the result in buildStorage, which
    /// is normally absent (both checked in the game's IL, 2026-11-06).  So the
    /// patch measures, for the same craft: the dish's calories (gross), the
    /// edible calories the ingredients took out of buildStorage (inputs), and
    /// whatever the game itself reported meanwhile (reported).
    /// </summary>
    internal static class CookedFoodPatch
    {
        [ThreadStatic]
        private static int depth;

        // Set by the outermost prefix, read by the outermost postfix.
        [ThreadStatic]
        private static double edibleBefore;

        [ThreadStatic]
        private static double reportBefore;

        [ThreadStatic]
        private static bool measured;

        internal static void Install(Harmony harmony)
        {
            try
            {
                HarmonyMethod prefix = new HarmonyMethod(AccessTools.Method(typeof(CookedFoodPatch), nameof(Enter)));
                HarmonyMethod postfix = new HarmonyMethod(AccessTools.Method(typeof(CookedFoodPatch), nameof(Count)));
                HarmonyMethod finalizer = new HarmonyMethod(AccessTools.Method(typeof(CookedFoodPatch), nameof(Leave)));
                int patched = 0;
                foreach (Type type in typeof(ComplexFabricator).Assembly.GetTypes())
                {
                    if (!typeof(ComplexFabricator).IsAssignableFrom(type))
                    {
                        continue;
                    }
                    MethodInfo target = AccessTools.DeclaredMethod(type, "SpawnOrderProduct", new[] { typeof(ComplexRecipe) });
                    if (target == null || target.IsAbstract || target.ReturnType != typeof(List<GameObject>))
                    {
                        continue;
                    }
                    harmony.Patch(target, prefix: prefix, postfix: postfix, finalizer: finalizer);
                    patched++;
                }
                if (patched == 0)
                {
                    ApiLog.Error("patch_cooked_food", "ComplexFabricator.SpawnOrderProduct(ComplexRecipe) was not found.");
                }
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_cooked_food", ex);
            }
        }

        private static void Enter(ComplexFabricator __instance)
        {
            depth++;
            if (depth != 1)
            {
                return;
            }
            measured = false;
            try
            {
                ReportManager.DailyReport today = TodaysReport();
                if (today == null || __instance == null)
                {
                    return;
                }
                edibleBefore = EdibleCalories(__instance.buildStorage, null);
                reportBefore = today.GetEntry(ReportManager.ReportType.CaloriesCreated).Net;
                measured = true;
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_cooked_food", ex);
            }
        }

        private static void Count(ComplexFabricator __instance, List<GameObject> __result)
        {
            if (depth != 1 || !measured || __result == null || GameClock.Instance == null)
            {
                return;
            }
            try
            {
                ReportManager.DailyReport today = TodaysReport();
                if (today == null)
                {
                    return;
                }
                double gross = 0.0;
                HashSet<GameObject> products = new HashSet<GameObject>();
                foreach (GameObject product in __result)
                {
                    if (product == null)
                    {
                        continue;
                    }
                    products.Add(product);
                    Edible edible = product.GetComponent<Edible>();
                    if (edible != null)
                    {
                        gross += edible.Calories;
                    }
                }
                // Products a station keeps in buildStorage are not ingredients.
                double inputs = Math.Max(0.0, edibleBefore - EdibleCalories(__instance.buildStorage, products));
                double reported = today.GetEntry(ReportManager.ReportType.CaloriesCreated).Net - reportBefore;
                if (gross > 0.0 || inputs > 0.0 || reported != 0.0)
                {
                    GameClock clock = GameClock.Instance;
                    ApiFoodLedger.Add(Game.Instance, today.day, clock.GetTimeInCycles(), gross, inputs, reported);
                }
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_cooked_food", ex);
            }
        }

        // A finalizer runs even when the method throws, so depth never sticks.
        private static void Leave()
        {
            if (depth > 0)
            {
                depth--;
            }
        }

        private static ReportManager.DailyReport TodaysReport()
        {
            ReportManager reports = ReportManager.Instance;
            return ReferenceEquals(reports, null) ? null : reports.TodaysReport;
        }

        private static double EdibleCalories(Storage storage, HashSet<GameObject> skip)
        {
            double total = 0.0;
            if (storage == null)
            {
                return total;
            }
            foreach (GameObject item in storage.items)
            {
                if (item == null || (skip != null && skip.Contains(item)))
                {
                    continue;
                }
                Edible edible = item.GetComponent<Edible>();
                if (edible != null && !float.IsNaN(edible.Calories) && !float.IsInfinity(edible.Calories))
                {
                    total += edible.Calories;
                }
            }
            return total;
        }
    }
}
