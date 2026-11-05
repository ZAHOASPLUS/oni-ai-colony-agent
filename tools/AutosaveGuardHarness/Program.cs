using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;
using OniAiProbeMod.Patches;
using OniAiProbeMod.Probe;

namespace AutosaveGuardHarness
{
    // Code review 2026-09-26, P0-1: a validation process must never let ONI
    // autosave, because autosave rotation deletes the user's own colony
    // autosaves.  This drives AutosaveGuard against the real SaveGame and
    // GameClock types (allocated without Unity, never initialized) and checks
    // that the GameClock.DoAutoSave prefix refuses the autosave.
    internal static class Program
    {
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            // Keep the log away from Documents\Klei.
            string bridge = Path.Combine(Path.GetTempPath(), "oni-autosave-guard-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(bridge);
            Environment.SetEnvironmentVariable("ONI_AI_BRIDGE_DIR", bridge);
            ApiLog.Initialize();
            try
            {
                Run(bridge);
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.autosave-guard-harness.v1",
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

        private static void Run(string bridge)
        {
            BuildingSettingsTests.Run(Passed);
            MethodInfo doAutoSave = AccessTools.Method(typeof(GameClock), "DoAutoSave", new[] { typeof(int) });
            Require(doAutoSave != null, "GameClock.DoAutoSave(int) is missing from the game assembly");
            MethodInfo patchSuppression = typeof(LifecyclePatches).GetMethod("PatchAutosaveSuppression", AnyStatic);
            MethodInfo onSaveStarted = typeof(LifecyclePatches).GetMethod("OnSaveStarted", AnyStatic);
            Require(patchSuppression != null && onSaveStarted != null, "LifecyclePatches hooks are missing");
            Harmony harmony = new Harmony("oni.autosave-guard-harness");

            // Normal play: API mode is off, so nothing may change.
            Require(!AutosaveGuard.IsActive, "guard armed without API mode");
            SaveGame userSave = LoadSave(1);
            AutosaveGuard.OnSessionStarted();
            Require(AutosaveGuard.Tick(100f) == null && !AutosaveGuard.ShouldSuppressAutosave(), "inactive guard acted");
            Require(userSave.AutoSaveCycleInterval == 1, "inactive guard changed the user's interval");
            JObject inactive = Describe();
            Require((string)inactive["status"] == "inactive" && !(bool)inactive["guard_active"] &&
                (int)inactive["cycle_interval"] == 1 && inactive["enforced_interval"].Type == JTokenType.Null,
                "inactive description is wrong: " + inactive);
            patchSuppression.Invoke(null, new object[] { harmony });
            Require(Harmony.GetPatchInfo(doAutoSave) == null, "DoAutoSave was patched without a validation binding");
            Passed.Add("inactive_without_binding");

            AutosaveGuard.Activate("harness");
            SaveGame.Instance = null;
            AutosaveGuard.OnSessionStarted();
            JObject unavailable = Describe();
            Require((string)unavailable["status"] == "save_unavailable" &&
                unavailable["cycle_interval"].Type == JTokenType.Null &&
                unavailable["interval_at_session_start"].Type == JTokenType.Null,
                "missing SaveGame description is wrong: " + unavailable);
            Passed.Add("save_unavailable");

            // A load deserializes the save's own interval; the session start forces 0.
            SaveGame clone = LoadSave(3);
            AutosaveGuard.OnSessionStarted();
            JObject loaded = Describe();
            Require(clone.AutoSaveCycleInterval == 0, "session start left autosave enabled");
            Require((int)loaded["interval_at_session_start"] == 3 && (int)loaded["cycle_interval"] == 0 &&
                (int)loaded["enforced_interval"] == 0 && (long)loaded["enforcement_count"] == 1 &&
                (string)loaded["status"] == "suppression_patch_missing",
                "session start description is wrong: " + loaded);
            Passed.Add("session_start_forces_zero");

            // The game's Harmony cannot rewrite methods on this .NET runtime, so
            // the harness accepts either outcome but requires it to be reported
            // honestly: a failed install must never read as "enforced".
            patchSuppression.Invoke(null, new object[] { harmony });
            bool harmonyWorks = HarmonyWorksHere(harmony);
            string installed = (string)Describe()["status"];
            if (harmonyWorks)
            {
                Patches info = Harmony.GetPatchInfo(doAutoSave);
                Require(info != null && info.Prefixes.Count == 1 &&
                    info.Prefixes[0].PatchMethod.Name == "OnAutoSaveRequested" && installed == "enforced",
                    "DoAutoSave suppression prefix was not installed");
                Passed.Add("suppression_patch_installed");
            }
            else
            {
                Require(installed == "suppression_patch_missing", "a failed patch install was reported as " + installed);
                Passed.Add("suppression_patch_failure_reported");
            }

            // Harmony binds prefix arguments by name: the prefix must take the
            // original's (int day) and return bool to be able to skip it.
            MethodInfo prefix = typeof(LifecyclePatches).GetMethod("OnAutoSaveRequested", AnyStatic);
            ParameterInfo[] originalParameters = doAutoSave.GetParameters();
            ParameterInfo[] prefixParameters = prefix == null ? new ParameterInfo[0] : prefix.GetParameters();
            Require(prefix != null && prefix.ReturnType == typeof(bool) &&
                originalParameters.Length == 1 && prefixParameters.Length == 1 &&
                originalParameters[0].Name == prefixParameters[0].Name &&
                originalParameters[0].ParameterType == prefixParameters[0].ParameterType,
                "the prefix signature does not bind to GameClock.DoAutoSave");
            Passed.Add("prefix_binds_do_autosave");
            if (!harmonyWorks)
            {
                AutosaveGuard.MarkSuppressionPatchInstalled();
            }
            Require((string)Describe()["status"] == "enforced", "installed guard is not reported as enforced");

            // The options screen writes the interval directly; the throttled tick restores 0.
            clone.AutoSaveCycleInterval = 5;
            Require((string)Describe()["status"] == "violated", "a re-enabled interval was not reported");
            Require(AutosaveGuard.Tick(0.2f) == 5 && clone.AutoSaveCycleInterval == 0, "tick did not restore 0");
            clone.AutoSaveCycleInterval = 2;
            Require(AutosaveGuard.Tick(0.7f) == null && clone.AutoSaveCycleInterval == 2, "tick was not throttled");
            Require(AutosaveGuard.Tick(1.3f) == 2 && clone.AutoSaveCycleInterval == 0, "throttled tick did not restore 0");
            Passed.Add("tick_restores_zero");

            // Every save the session writes serializes interval 0.
            clone.AutoSaveCycleInterval = 4;
            onSaveStarted.Invoke(null, null);
            Require(clone.AutoSaveCycleInterval == 0, "a save write started with autosave enabled");
            Passed.Add("save_write_serializes_zero");

            // An autosave that still fires is refused before the original runs.
            clone.AutoSaveCycleInterval = 1;
            if (harmonyWorks)
            {
                GameClock clock = (GameClock)RuntimeHelpers.GetUninitializedObject(typeof(GameClock));
                doAutoSave.Invoke(clock, new object[] { 12 });
            }
            else
            {
                Require(!(bool)prefix.Invoke(null, new object[] { 12 }), "the prefix let the autosave run");
            }
            JObject suppressed = Describe();
            Require((long)suppressed["suppressed_autosave_count"] == 1 && clone.AutoSaveCycleInterval == 0,
                "DoAutoSave was not refused: " + suppressed);
            Passed.Add("do_autosave_refused");

            // A reload brings the save's interval back; the next session start forces 0 again.
            SaveGame reloaded = LoadSave(1);
            AutosaveGuard.OnSessionStarted();
            JObject reload = Describe();
            Require(reloaded.AutoSaveCycleInterval == 0 && (int)reload["interval_at_session_start"] == 1 &&
                (string)reload["status"] == "enforced", "reload re-enabled autosave: " + reload);
            Passed.Add("reload_forces_zero");

            List<JObject> records = File.ReadAllLines(Path.Combine(bridge, "api-log.jsonl"))
                .Where(line => line.Length > 0)
                .Select(JObject.Parse)
                .ToList();
            JObject patchError = records.SingleOrDefault(record =>
                (string)record["event"] == "error" && (string)record["stage"] == "patch_autosave_suppression");
            JObject refused = records.SingleOrDefault(record => (string)record["event"] == "autosave_suppressed");
            Require(harmonyWorks ? patchError == null : patchError != null,
                "the suppression patch outcome was not recorded");
            Require(refused != null && (int)refused["clock_cycle"] == 12 &&
                (string)refused["autosave_status"] == "enforced", "the refused autosave was not recorded");
            Passed.Add("events_recorded");
        }

        private static SaveGame LoadSave(int interval)
        {
            SaveGame save = (SaveGame)RuntimeHelpers.GetUninitializedObject(typeof(SaveGame));
            save.AutoSaveCycleInterval = interval;
            SaveGame.Instance = save;
            return save;
        }

        private static JObject Describe()
        {
            // Built by hand: the game's Newtonsoft build needs .NET Framework
            // permissions for reflection-based serialization.
            JObject result = new JObject();
            foreach (KeyValuePair<string, object> entry in AutosaveGuard.Describe())
            {
                result[entry.Key] = entry.Value == null ? JValue.CreateNull() : new JValue(entry.Value);
            }
            return result;
        }

        // A patched original only stays unexecuted if Harmony can rewrite
        // methods on this runtime; prove that on a harmless method before the
        // real DoAutoSave (which would touch the game's settings) is invoked.
        private static bool HarmonyWorksHere(Harmony harmony)
        {
            try
            {
                MethodInfo target = typeof(Program).GetMethod(nameof(Canary), AnyStatic);
                harmony.Patch(target, prefix: new HarmonyMethod(typeof(Program).GetMethod(nameof(SkipCanary), AnyStatic)));
                return Canary() == 0;
            }
            catch
            {
                return false;
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static int Canary()
        {
            return 1;
        }

        private static bool SkipCanary(ref int __result)
        {
            __result = 0;
            return false;
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
