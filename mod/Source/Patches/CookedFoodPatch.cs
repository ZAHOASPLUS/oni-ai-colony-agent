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
    /// Musher, Electric Grill, Gas Range, Deep Fryer, Sushi Bar ...).  The patch
    /// adds the calories of what it returns to ApiFoodLedger.  Overrides call
    /// down to the base, so only the outermost call on the stack counts.
    /// </summary>
    internal static class CookedFoodPatch
    {
        [ThreadStatic]
        private static int depth;

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

        private static void Enter()
        {
            depth++;
        }

        private static void Count(List<GameObject> __result)
        {
            if (depth != 1 || __result == null || GameClock.Instance == null)
            {
                return;
            }
            ReportManager reports = ReportManager.Instance;
            if (ReferenceEquals(reports, null) || reports.TodaysReport == null)
            {
                return;
            }
            try
            {
                double calories = 0.0;
                foreach (GameObject product in __result)
                {
                    Edible edible = product == null ? null : product.GetComponent<Edible>();
                    if (edible != null)
                    {
                        calories += edible.Calories;
                    }
                }
                if (calories > 0.0)
                {
                    GameClock clock = GameClock.Instance;
                    ApiFoodLedger.Add(Game.Instance, reports.TodaysReport.day, clock.GetTimeInCycles(), calories);
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
    }
}
