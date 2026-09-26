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

        private static void OnGameStarted()
        {
            if (saveLoadInProgress)
            {
                return;
            }
            ProbeRunner.EnsureStarted("game_lifecycle_patch");
        }

        private static void OnGameStopped()
        {
            ProbeRunner.Stop("game_lifecycle_patch");
        }

        private static void OnSaveLoadStarting(string filename)
        {
            saveLoadInProgress = true;
            ProbeRunner.Stop("save_loader_load_start");
            ProbeSession.PrepareSaveLoad(filename);
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
            __state = ProbeSession.BeginSaveWrite(filename);
            ProbeWriter.WriteEvent("save_write_started", new Dictionary<string, object>
            {
                { "is_auto_save", isAutoSave },
                { "update_save_pointer", updateSavePointer },
                { "requested_save_path_id", __state.RequestedPathId },
                { "before_save_id_hash", __state.BeforeSaveIdHash }
            });
        }

        private static void OnSaveFinished(
            string filename,
            bool isAutoSave,
            bool updateSavePointer,
            string __result,
            ProbeSession.SaveWriteContext __state)
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
    }
}
