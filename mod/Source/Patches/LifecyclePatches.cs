using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using OniAiProbeMod.Api;
using OniAiProbeMod.Probe;
using OniAiProbeMod.Agent;
using UnityEngine;

namespace OniAiProbeMod.Patches
{
    internal static class LifecyclePatches
    {
        internal static void Install(Harmony harmony)
        {
            PatchSessionTracking(harmony);
            PatchAutosaveSuppression(harmony);
            PatchTimelapseSuppression(harmony);
            PatchModDialogSuppression(harmony);
            PatchIntroShortSkip(harmony);
        }

        // The main menu plays an intro short once per release, and a player has
        // to click through it.  The prefix leaves the "already played" preference
        // alone, so a later normal launch still shows it.
        private static void PatchIntroShortSkip(Harmony harmony)
        {
            if (!ApiMode.Enabled)
            {
                return;
            }
            try
            {
                MethodInfo target = AccessTools.Method(typeof(MainMenuIntroShort), "OnSpawn");
                if (target == null)
                {
                    ApiLog.Error("patch_intro_short_skip", "MainMenuIntroShort.OnSpawn was not found.");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnIntroShortSpawn))));
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_intro_short_skip", ex);
            }
        }

        private static bool OnIntroShortSpawn(MainMenuIntroShort __instance)
        {
            __instance.gameObject.SetActive(false);
            return false;
        }

        // A timelapse screenshot queued for a world that is no longer loaded never
        // completes, so the queue never drains and the simulation clock stops
        // advancing (2026-10-25 rounds 13, 15, 17).  The guard must not touch the
        // queue itself: clearing it while the Render coroutine iterates by index
        // threw ArgumentOutOfRangeException and ended the session (round 19).  The
        // prefix simply refuses Timelapser.Update, so no render is ever started.
        private static void PatchTimelapseSuppression(Harmony harmony)
        {
            if (!TimelapseGuard.IsActive)
            {
                return;
            }
            try
            {
                MethodInfo target = AccessTools.Method(typeof(Timelapser), "Update", Type.EmptyTypes);
                if (target == null)
                {
                    ApiLog.Error("patch_timelapse_suppression", "Timelapser.Update() was not found.");
                    Debug.LogWarning(Mod.LogPrefix + " Timelapser.Update() was not found");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnTimelapserUpdate))));
                TimelapseGuard.MarkSuppressionPatchInstalled();
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_timelapse_suppression", ex);
            }
        }

        private static bool OnTimelapserUpdate()
        {
            try
            {
                return !TimelapseGuard.ShouldSuppressUpdate();
            }
            catch
            {
                // Only installed for a validation process: refuse on any doubt.
                return false;
            }
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
            if (!ApiMode.Enabled)
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
                    ApiLog.Error("patch_mod_dialog_suppression", "KMod.Manager.NotifyDialog was not found.");
                    Debug.LogWarning(Mod.LogPrefix + " KMod.Manager.NotifyDialog was not found");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnModNotifyDialog))));
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_mod_dialog_suppression", ex);
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
                ApiLog.Event("mod_dialog_suppressed", new Dictionary<string, object>
                {
                    { "dialog", "save_game_mods_differ" },
                    { "events", recorded }
                });
                Debug.Log(Mod.LogPrefix + " Save-game mod footprint dialog recorded and skipped (" + recorded.Count + " events)");
                return false;
            }
            catch (Exception ex)
            {
                ApiLog.Error("mod_dialog_suppression", ex);
                return true;
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
                    ApiLog.Error("patch_autosave_suppression", "GameClock.DoAutoSave(int) was not found.");
                    Debug.LogWarning(Mod.LogPrefix + " GameClock.DoAutoSave(int) was not found");
                    return;
                }

                harmony.Patch(
                    target,
                    prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnAutoSaveRequested))));
                AutosaveGuard.MarkSuppressionPatchInstalled();
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_autosave_suppression", ex);
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
                ApiLog.Event("autosave_suppressed", new Dictionary<string, object>
                {
                    { "clock_cycle", day },
                    { "autosave_status", AutosaveGuard.Describe()["status"] }
                });
            }
            catch
            {
                // The autosave is refused either way.
            }
            return false;
        }

        // A session starts when SaveLoader.Load has returned true, not at
        // Game.OnSpawn, which runs from inside the load.
        private static void PatchSessionTracking(Harmony harmony)
        {
            try
            {
                MethodInfo load = AccessTools.Method(typeof(SaveLoader), "Load", new[] { typeof(string) });
                MethodInfo save = AccessTools.Method(
                    typeof(SaveLoader),
                    "Save",
                    new[] { typeof(string), typeof(bool), typeof(bool) });
                MethodInfo stop = AccessTools.Method(typeof(Game), "OnDestroy");
                if (load == null || save == null || stop == null)
                {
                    ApiLog.Error("patch_session_tracking", "SaveLoader.Load, SaveLoader.Save or Game.OnDestroy was not found.");
                    return;
                }
                harmony.Patch(save, prefix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveStarted))));
                harmony.Patch(load, postfix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnSaveLoaded))));
                harmony.Patch(stop, postfix: new HarmonyMethod(AccessTools.Method(typeof(LifecyclePatches), nameof(OnGameStopped))));
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_session_tracking", ex);
            }
        }

        private static void OnSaveLoaded(bool __result)
        {
            ApiLifecycle.SessionActive = __result;
            ApiEvents.ResetSession();
            ApiPrintables.ResetSession();
            AgentHost.NotifySessionChanged(__result);
            if (__result)
            {
                try
                {
                    AutosaveGuard.OnSessionStarted();
                }
                catch (Exception ex)
                {
                    ApiLog.Error("autosave_session_start", ex);
                }
            }
        }

        // Serialize interval 0 into every save, so a checkpoint cannot autosave
        // when it is loaded later.
        private static void OnSaveStarted()
        {
            try
            {
                AutosaveGuard.EnforceNow();
            }
            catch (Exception ex)
            {
                ApiLog.Error("autosave_save_start", ex);
            }
        }

        private static void OnGameStopped()
        {
            ApiLifecycle.SessionActive = false;
            ApiEvents.ResetSession();
            ApiPrintables.ResetSession();
            AgentHost.NotifySessionChanged(false);
        }
    }
}
