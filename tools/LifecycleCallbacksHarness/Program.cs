using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Patches;
using OniAiProbeMod.Probe;

namespace LifecycleCallbacksHarness
{
    // Code review 2026-09-26 (P2): an exception escaping a Harmony callback
    // propagates into the patched game method (SaveLoader.Load/Save,
    // Game.OnSpawn, GameClock.DoAutoSave, SpeedControlScreen.Pause/Unpause).
    // Every callback the Mod registers must contain a catch-all boundary, and a
    // reported failure must be recorded and stop the controllers.
    internal static class Program
    {
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;

        // Every method LifecyclePatches hands to Harmony.
        private static readonly string[] Callbacks =
        {
            "OnGameStarted",
            "OnGameStopped",
            "OnModNotifyDialog",
            "OnSaveLoadStarting",
            "OnSaveLoadFinished",
            "OnSaveStarted",
            "OnSaveFinished",
            "OnAutoSaveRequested",
            "OnPauseChanging",
            "OnPauseChanged",
            // TimelapseGuard's Timelapser.Update prefix (2026-10-25 round 18).
            "OnTimelapserUpdate"
        };

        private static int Main()
        {
            string bridge = Path.Combine(Path.GetTempPath(), "oni-lifecycle-callbacks-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(bridge);
            Environment.SetEnvironmentVariable("ONI_AI_BRIDGE_DIR", bridge);
            try
            {
                List<string> passed = Run(bridge);
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.lifecycle-callbacks-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(passed.ToArray())
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

        private static List<string> Run(string bridge)
        {
            List<string> passed = new List<string>();
            // Harmony callbacks are the static On* methods named in nameof(...)
            // registrations; a new one must be added to the list above.
            string[] declared = typeof(LifecyclePatches).GetMethods(AnyStatic)
                .Where(method => method.Name.StartsWith("On", StringComparison.Ordinal))
                .Select(method => method.Name)
                .OrderBy(name => name, StringComparer.Ordinal)
                .ToArray();
            Require(declared.SequenceEqual(Callbacks.OrderBy(name => name, StringComparer.Ordinal)),
                "LifecyclePatches callbacks changed: " + string.Join(",", declared));
            foreach (string name in Callbacks)
            {
                MethodInfo callback = typeof(LifecyclePatches).GetMethod(name, AnyStatic);
                bool catchesAll = callback.GetMethodBody().ExceptionHandlingClauses.Any(clause =>
                    clause.Flags == ExceptionHandlingClauseOptions.Clause &&
                    (clause.CatchType == typeof(Exception) || clause.CatchType == typeof(object)));
                Require(catchesAll, name + " has no catch-all exception boundary");
            }
            passed.Add("every_callback_catches");

            Require(!ProbeWriter.HasSafetyFault, "the harness started with a safety fault");
            LifecyclePatches.ReportCallbackFailure("harness", new InvalidOperationException("boom"));
            Require(ProbeWriter.HasSafetyFault, "a callback failure did not raise the safety fault");
            passed.Add("failure_raises_safety_fault");

            ProbeWriter.Shutdown();
            List<JObject> records = File.ReadAllLines(Path.Combine(bridge, "state.jsonl"))
                .Where(line => line.Length > 0)
                .Select(JObject.Parse)
                .ToList();
            Require(records.Any(record =>
                    (string)record["event"] == "probe_error" &&
                    (string)record["stage"] == "lifecycle_harness" &&
                    (string)record["category"] == "exception"),
                "the callback failure was not recorded");
            passed.Add("failure_recorded");
            return passed;
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
