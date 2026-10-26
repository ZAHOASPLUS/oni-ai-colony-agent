using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using OniAiProbeMod.Probe;
using UnityEngine;

namespace OniAiProbeMod.Patches
{
    internal static class LifecyclePatches
    {
        // Game.OnSpawn can run from inside SaveLoader.Load before the Harmony
        // postfix has observed the successful return value.  Starting the probe
        // from that nested callback would erase the only trustworthy distinction
        // between a requested save and a fully completed load.  Keep the generic
        // lifecycle callback fail-closed until the load postfix owns the start.
        private static bool saveLoadInProgress;

        private static readonly string[] StartMethods =
        {
            "OnSpawn",
            "Start",
            "OnPrefabInit"
        };

        private static readonly string[] StopMethods =
        {
            "OnCleanUp",
            "OnDestroy"
        };

        internal static void Install(Harmony harmony)
        {
            PatchFirst(harmony, "Game", StartMethods, nameof(OnGameStarted), "game_start");
            PatchFirst(harmony, "Game", StopMethods, nameof(OnGameStopped), "game_stop");
            PatchSaveLifecycle(harmony);
            PatchAutosaveSuppression(harmony);
            PatchPauseLedger(harmony);
            PatchModDialogSuppression(harmony);
        }

        // A validation process loads a clone that was saved under a different
        // mod footprint, so Game.OnSpawn raises the "save game mods differ"
        // dialog on every run.  Dismissing it needed a GUI click, which cost run
        // m1-0930a (a scripted window resize hung the game).  For an accepted
        // startup binding the footprint events are recorded as evidence and
        // that one dialog is skipped; every other mod event keeps its dialog,
        // and normal play is not patched at all.
        private static void PatchModDialogSuppression(Harmony harmony)
        {
            if (!StartupBinding.WasAccepted)
            {
                return;
            }
            try
            {
                MethodInfo target = AccessTools.Method(
                    typeof(KMod.Manager),
                    "NotifyDialog",
                    new[] { typeof(string), typeof(string), typeof(GameObject) });
                if (target == null)
                {
                    ProbeWriter.WriteError("patch_mod_dialog_suppression", "KMod.Manager.NotifyDialog was not found.");
                    Debug.LogWarning(Mod.LogPrefix + " KMod.Manager.NotifyDialog was not found");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnModNotifyDialog))));
                ProbeWriter.WriteEvent("probe_patch_registered", new Dictionary<string, object>
                {
                    { "target", "KMod.Manager.NotifyDialog" },
                    { "patch", "mod_dialog_suppression" }
                });
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("patch_mod_dialog_suppression", ex);
            }
        }

        private static bool OnModNotifyDialog(KMod.Manager __instance, string title)
        {
            try
            {
                List<KMod.Event> events = __instance == null ? null : __instance.events;
                if (events == null || events.Count == 0)
                {
                    return true;
                }
                string differTitle = STRINGS.UI.FRONTEND.MOD_DIALOGS.SAVE_GAME_MODS_DIFFER.TITLE;
                if (!string.Equals(title, differTitle, StringComparison.Ordinal))
                {
                    return true;
                }

                List<object> recorded = new List<object>();
                foreach (KMod.Event item in events)
                {
                    if (item.event_type != KMod.EventType.ExpectedActive &&
                        item.event_type != KMod.EventType.ExpectedInactive)
                    {
                        // Anything beyond a footprint difference is shown as usual.
                        return true;
                    }
                    string modTitle = item.mod.title ?? string.Empty;
                    recorded.Add(new Dictionary<string, object>
                    {
                        { "event_type", item.event_type.ToString() },
                        { "mod_id", item.mod.id ?? string.Empty },
                        { "mod_title", modTitle.Length > 64 ? modTitle.Substring(0, 64) : modTitle },
                        { "mod_version", item.mod.version },
                        { "distribution_platform", item.mod.distribution_platform.ToString() }
                    });
                }

                events.Clear();
                ProbeWriter.WriteEvent("mod_dialog_suppressed", new Dictionary<string, object>
                {
                    { "dialog", "save_game_mods_differ" },
                    { "events", recorded }
                });
                Debug.Log(Mod.LogPrefix + " Save-game mod footprint dialog recorded and skipped (" + recorded.Count + " events)");
                return false;
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("mod_dialog_suppression", ex);
                return true;
            }
        }

        // The executor only runs for an accepted startup binding; it needs the
        // pause reference count to avoid taking a modal's pause (PauseLedger).
        private static void PatchPauseLedger(Harmony harmony)
        {
            if (!StartupBinding.WasAccepted)
            {
                return;
            }
            try
            {
                MethodInfo pause = AccessTools.Method(
                    typeof(SpeedControlScreen),
                    "Pause",
                    new[] { typeof(bool), typeof(bool) });
                MethodInfo unpause = AccessTools.Method(typeof(SpeedControlScreen), "Unpause", new[] { typeof(bool) });
                if (pause == null || unpause == null)
                {
                    ProbeWriter.WriteError("patch_pause_ledger", "SpeedControlScreen.Pause/Unpause method was not found.");
                    return;
                }

                HarmonyMethod prefix = new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnPauseChanging)));
                HarmonyMethod postfix = new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnPauseChanged)));
                harmony.Patch(pause, prefix: prefix, postfix: postfix);
                harmony.Patch(unpause, prefix: prefix, postfix: postfix);
                Commands.PauseLedger.MarkTracking();
                ProbeWriter.WriteEvent("probe_patch_registered", new Dictionary<string, object>
                {
                    { "target", "SpeedControlScreen.Pause/Unpause" },
                    { "patch", "pause_ledger" }
                });
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("patch_pause_ledger", ex);
            }
        }

        private static void OnPauseChanging(SpeedControlScreen __instance)
        {
            try
            {
                Commands.PauseLedger.BeginChange(__instance);
            }
            catch
            {
                // Never break the game's pause; an unobserved change stays unknown.
            }
        }

        private static void OnPauseChanged(SpeedControlScreen __instance, int ___pauseCount)
        {
            try
            {
                Commands.PauseLedger.Observe(__instance, ___pauseCount);
            }
            catch
            {
                // As above.
            }
        }

        // P0-1: GameClock.DoAutoSave is the only caller that saves with
        // isAutoSave=true, which is what runs the ten-per-colony autosave
        // rotation.  Patched only for a validation process, so normal play keeps
        // the game's own behaviour.
        private static void PatchAutosaveSuppression(Harmony harmony)
        {
            if (!AutosaveGuard.IsActive)
            {
                return;
            }
            try
            {
                MethodInfo target = AccessTools.Method(typeof(GameClock), "DoAutoSave", new[] { typeof(int) });
                if (target == null)
                {
                    ProbeWriter.WriteError("patch_autosave_suppression", "GameClock.DoAutoSave(int) was not found.");
                    Debug.LogWarning(Mod.LogPrefix + " GameClock.DoAutoSave(int) was not found");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnAutoSaveRequested))));
                AutosaveGuard.MarkSuppressionPatchInstalled();
                ProbeWriter.WriteEvent("probe_patch_registered", new Dictionary<string, object>
                {
                    { "target", "GameClock.DoAutoSave" },
                    { "patch", "autosave_suppression" }
                });
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("patch_autosave_suppression", ex);
            }
        }

        private static bool OnAutoSaveRequested(int day)
        {
            bool suppress;
            try
            {
                suppress = AutosaveGuard.ShouldSuppressAutosave();
            }
            catch
            {
                // Only installed for a validation process: refuse on any doubt.
                suppress = true;
            }
            if (!suppress)
            {
                return true;
            }
            try
            {
                ProbeWriter.WriteEvent("autosave_suppressed", new Dictionary<string, object>
                {
                    { "clock_cycle", day },
                    { "autosave", AutosaveGuard.Describe() }
                });
            }
            catch
            {
                // The autosave is refused either way.
            }
            return false;
        }

        private static void PatchSaveLifecycle(Harmony harmony)
        {
            try
            {
                MethodInfo load = AccessTools.Method(typeof(SaveLoader), "Load", new[] { typeof(string) });
                MethodInfo save = AccessTools.Method(
                    typeof(SaveLoader),
                    "Save",
                    new[] { typeof(string), typeof(bool), typeof(bool) });
                if (load == null || save == null)
                {
                    ProbeWriter.WriteError("patch_save_lifecycle", "SaveLoader.Load/Save method was not found.");
                    return;
                }

                harmony.Patch(
                    load,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveLoadStarting))),
                    postfix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveLoadFinished))));
                harmony.Patch(
                    save,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveStarted))),
                    postfix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveFinished))));
                ProbeWriter.WriteEvent("probe_patch_registered", new Dictionary<string, object>
                {
                    { "target", "SaveLoader.Load/Save" },
                    { "patch", "save_lifecycle" }
                });
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("patch_save_lifecycle", ex);
            }
        }

        private static void PatchFirst(Harmony harmony, string typeName, IEnumerable<string> methodNames, string callbackName, string patchName)
        {
            try
            {
                Type type = AccessTools.TypeByName(typeName);
                if (type == null)
                {
                    ProbeWriter.WriteError("patch_" + patchName, "Target type was not found: " + typeName);
                    Debug.LogWarning(Mod.LogPrefix + " Target type was not found: " + typeName);
                    return;
                }

                MethodInfo callback = AccessTools.Method(typeof(LifecyclePatches), callbackName);
                foreach (string methodName in methodNames)
                {
                    MethodBase target = AccessTools.Method(type, methodName);
                    if (target == null)
                    {
                        continue;
                    }

                    harmony.Patch(target, postfix: new HarmonyMethod(callback));
                    ProbeWriter.WriteEvent("probe_patch_registered", new Dictionary<string, object>
                    {
                        { "target", typeName + "." + methodName },
                        { "patch", patchName }
                    });
                    Debug.Log(Mod.LogPrefix + " Patched " + typeName + "." + methodName);
                    return;
                }

                ProbeWriter.WriteError("patch_" + patchName, "No lifecycle method was found on " + typeName);
                Debug.LogWarning(Mod.LogPrefix + " No lifecycle method was found on " + typeName);
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("patch_" + patchName, ex);
                Debug.LogWarning(Mod.LogPrefix + " Failed to register " + patchName + ": " + ex);
            }
        }

        // Code review 2026-09-26 (P2): an exception escaping a Harmony callback
        // propagates into the patched game method, so a Probe bug could fail
        // the player's load or save.  Every lifecycle callback catches, records
        // the failure, and raises the sticky safety fault: the executor stops,
        // because the Probe's session and save tracking can no longer be trusted.
        internal static void ReportCallbackFailure(string stage, Exception error)
        {
            try
            {
                ProbeWriter.WriteError("lifecycle_" + stage, error);
            }
            catch
            {
                // The fault below must still be raised.
            }
            try
            {
                ProbeWriter.RegisterLifecycleFault();
            }
            catch
            {
                // Never let the report itself reach the game.
            }
        }

        private static void OnGameStarted()
        {
            try
            {
                if (saveLoadInProgress)
                {
                    return;
                }
                ProbeRunner.EnsureStarted("game_lifecycle_patch");
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("game_start", ex);
            }
        }

        private static void OnGameStopped()
        {
            try
            {
                ProbeRunner.Stop("game_lifecycle_patch");
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("game_stop", ex);
            }
        }

        private static void OnSaveLoadStarting(string filename)
        {
            saveLoadInProgress = true;
            try
            {
                ProbeRunner.Stop("save_loader_load_start");
                ProbeSession.PrepareSaveLoad(filename);
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("save_load_start", ex);
            }
        }

        private static void OnSaveLoadFinished(bool __result)
        {
            try
            {
                if (__result)
                {
                    ProbeRunner.EnsureStarted("save_loader_load_complete");
                }
                else
                {
                    ProbeSession.CancelPreparedSaveLoad();
                }
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("save_load_finish", ex);
            }
            finally
            {
                saveLoadInProgress = false;
            }
        }

        private static void OnSaveStarted(
            string filename,
            bool isAutoSave,
            bool updateSavePointer,
            out ProbeSession.SaveWriteContext __state)
        {
            // A null context makes CompleteSaveWrite report the save as unverified.
            __state = null;
            try
            {
                // Serialize interval 0 into every save a validation session writes,
                // so a checkpoint clone cannot autosave when it is loaded later.
                AutosaveGuard.EnforceNow();
                __state = ProbeSession.BeginSaveWrite(filename);
                ProbeWriter.WriteEvent("save_write_started", new Dictionary<string, object>
                {
                    { "is_auto_save", isAutoSave },
                    { "update_save_pointer", updateSavePointer },
                    { "requested_save_path_id", __state.RequestedPathId },
                    { "before_save_id_hash", __state.BeforeSaveIdHash }
                });
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("save_write_start", ex);
            }
        }

        private static void OnSaveFinished(
            string filename,
            bool isAutoSave,
            bool updateSavePointer,
            string __result,
            ProbeSession.SaveWriteContext __state)
        {
            try
            {
                ProbeSession.SaveWriteCompletion completion = ProbeSession.CompleteSaveWrite(
                    __state,
                    __result,
                    updateSavePointer);
                ProbeWriter.WriteEvent("save_write_completed", new Dictionary<string, object>
                {
                    { "success", completion.Success },
                    { "is_auto_save", isAutoSave },
                    { "update_save_pointer", updateSavePointer },
                    { "file_rewrite_observed", completion.FileRewriteObserved },
                    { "save_file_readable", completion.SaveFileReadable },
                    { "failure_reason", completion.FailureReason },
                    { "requested_save_path_id", completion.RequestedPathId },
                    { "result_save_path_id", completion.ResultPathId },
                    { "active_save_path_id", completion.ActivePathId },
                    { "before_save_id_hash", completion.BeforeSaveIdHash },
                    { "written_save_id_hash", completion.WrittenSaveIdHash },
                    { "active_save_id_hash", completion.ActiveSaveIdHash }
                });
            }
            catch (Exception ex)
            {
                ReportCallbackFailure("save_write_finish", ex);
            }
        }
    }
}
