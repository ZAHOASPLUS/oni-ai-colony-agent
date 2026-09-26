using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;
using OniAiProbeMod.Patches;

namespace PauseLedgerHarness
{
    // Code review 2026-09-26 (P2): ONI's pause is reference counted, and the
    // executor's unpause took a modal's pause reference.  This checks that the
    // executor refuses an unpause unless the pause is held exactly once, using
    // the real SpeedControlScreen type (allocated without Unity) and the Mod's
    // own Pause/Unpause patch callbacks.
    internal static class Program
    {
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
        private const BindingFlags AnyInstance = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            // Keep ProbeWriter away from Documents\Klei should anything write.
            string bridge = Path.Combine(Path.GetTempPath(), "oni-pause-ledger-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(bridge);
            Environment.SetEnvironmentVariable("ONI_AI_BRIDGE_DIR", bridge);
            try
            {
                Run();
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.pause-ledger-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(Passed.ToArray())
                }.ToString(Newtonsoft.Json.Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
            finally
            {
                try
                {
                    Directory.Delete(bridge, true);
                }
                catch
                {
                    // Temporary directory only.
                }
            }
        }

        private static void Run()
        {
            // The patch targets and the injected private field exist in this game build.
            FieldInfo countField = typeof(SpeedControlScreen).GetField("pauseCount", AnyInstance);
            Require(countField != null && countField.FieldType == typeof(int), "SpeedControlScreen.pauseCount is missing");
            Require(typeof(SpeedControlScreen).GetMethod("Pause", new[] { typeof(bool), typeof(bool) }) != null &&
                typeof(SpeedControlScreen).GetMethod("Unpause", new[] { typeof(bool) }) != null,
                "SpeedControlScreen.Pause/Unpause signatures changed");
            MethodInfo changing = typeof(LifecyclePatches).GetMethod("OnPauseChanging", AnyStatic);
            MethodInfo changed = typeof(LifecyclePatches).GetMethod("OnPauseChanged", AnyStatic);
            Require(changing != null && changed != null, "pause ledger patch callbacks are missing");
            ParameterInfo[] changedParameters = changed.GetParameters();
            Require(changedParameters.Length == 2 &&
                changedParameters[0].Name == "__instance" && changedParameters[0].ParameterType == typeof(SpeedControlScreen) &&
                changedParameters[1].Name == "___" + countField.Name && changedParameters[1].ParameterType == typeof(int),
                "the postfix does not bind __instance and ___pauseCount");
            Passed.Add("patch_binds_pause_count");

            MethodInfo describe = typeof(ActionExecutor).GetMethod("DescribeUnpauseRefusal", AnyStatic);
            Require(describe != null, "ActionExecutor.DescribeUnpauseRefusal is missing");
            Func<string> refusal = () => (string)describe.Invoke(null, null);
            SpeedControlScreen screen = NewScreen(countField, 0);

            Require(refusal() == null, "an unpaused game was refused");
            Passed.Add("unpaused_game_not_refused");

            countField.SetValue(screen, 1);
            Require(screen.IsPaused, "IsPaused does not follow pauseCount");
            Require(Contains(refusal(), "not tracked"), "an untracked pause was not refused");
            PauseLedger.MarkTracking();
            Require(Contains(refusal(), "unknown"), "a pause never observed by the patch was not refused");
            Passed.Add("unknown_count_refused");

            // A modal opens over the player's pause: count 2.
            countField.SetValue(screen, 2);
            changing.Invoke(null, new object[] { screen });
            Require(Contains(refusal(), "unknown"), "a change in progress was not treated as unknown");
            changed.Invoke(null, new object[] { screen, 2 });
            Require(Contains(refusal(), "held 2 times"), "a pause held twice was not refused");
            Passed.Add("shared_pause_refused");

            // A reload creates a new screen; the old observation must not carry over.
            SpeedControlScreen reloaded = NewScreen(countField, 1);
            Require(Contains(refusal(), "unknown"), "an observation of the previous screen was reused");
            changed.Invoke(null, new object[] { reloaded, 1 });
            Require(PauseLedger.TryGetCount(reloaded, out int observed) && observed == 1, "the reloaded screen's count was not observed");
            Passed.Add("new_screen_not_inherited");

            // Held once: the count allows it, and the open-modal scan decides.
            Require(PauseLedger.CountRefusal(true, true, 1) == null, "a single pause reference was refused");
            Require(PauseLedger.ModalRefusal(0) == null, "no open modal was refused");
            Require(Contains(PauseLedger.ModalRefusal(1), "modal"), "an open pausing modal was not refused");
            Passed.Add("single_reference_and_modal_rules");
        }

        private static SpeedControlScreen NewScreen(FieldInfo countField, int pauseCount)
        {
            SpeedControlScreen screen = (SpeedControlScreen)RuntimeHelpers.GetUninitializedObject(typeof(SpeedControlScreen));
            countField.SetValue(screen, pauseCount);
            typeof(SpeedControlScreen).GetProperty("Instance", AnyStatic).GetSetMethod(true).Invoke(null, new object[] { screen });
            return screen;
        }

        private static bool Contains(string text, string part)
        {
            return text != null && text.IndexOf(part, StringComparison.Ordinal) >= 0;
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}
