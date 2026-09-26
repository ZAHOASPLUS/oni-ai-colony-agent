using System;
using System.Collections.Generic;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Keeps ONI's cycle autosave off for the whole of a validation process.
    ///
    /// Code review 2026-09-26, P0-1: an autosave keeps only the newest ten
    /// autosaves of the colony GUID in the active save folder's auto_save
    /// directory (SaveLoader.Save, MAX_AUTOSAVE_FILES) and deletes the rest with
    /// File.Delete.  A test clone is a byte copy with the user's colony GUID, and
    /// loading an autosave-derived clone redirects the active save path back to
    /// the original colony folder (SaveLoader.Load, isAutoSave+originalSaveName).
    /// Every clone autosave therefore deleted one of the user's own saves.
    ///
    /// GameClock only autosaves when SaveGame.AutoSaveCycleInterval &gt; 0, and
    /// that value is [Serialize]d into each save, so a load restores whatever the
    /// save held.  The guard therefore (1) writes 0 at every session start,
    /// (2) re-asserts it about once a second and before every save write, so the
    /// options screen cannot re-enable it and checkpoints persist 0, and (3) the
    /// GameClock.DoAutoSave prefix refuses any autosave that still gets through.
    /// It is armed only when a fresh startup binding was accepted, so normal play
    /// (no binding, or a stale one) is never changed.
    /// </summary>
    internal static class AutosaveGuard
    {
        internal const int EnforcedInterval = 0;
        internal const float EnforcementPeriodSeconds = 1f;

        internal const string StatusInactive = "inactive";
        internal const string StatusEnforced = "enforced";
        internal const string StatusSaveUnavailable = "save_unavailable";
        internal const string StatusPatchMissing = "suppression_patch_missing";
        internal const string StatusViolated = "violated";

        private static readonly object Sync = new object();
        private static bool active;
        private static string activationReason;
        private static bool suppressionPatchInstalled;
        private static int? intervalAtSessionStart;
        private static long enforcementCount;
        private static long suppressedAutosaveCount;
        private static string lastFailure;
        private static float nextEnforcementAt;

        internal static bool IsActive
        {
            get
            {
                lock (Sync)
                {
                    return active;
                }
            }
        }

        /// <summary>Arms the guard for the rest of this process.  There is no disarm.</summary>
        internal static void Activate(string reason)
        {
            lock (Sync)
            {
                if (active)
                {
                    return;
                }
                active = true;
                activationReason = reason;
            }
        }

        internal static void MarkSuppressionPatchInstalled()
        {
            lock (Sync)
            {
                suppressionPatchInstalled = true;
            }
        }

        /// <summary>
        /// Called synchronously from the load-complete path, before the game
        /// clock can advance: records what the save held and forces 0.
        /// </summary>
        internal static void OnSessionStarted()
        {
            lock (Sync)
            {
                intervalAtSessionStart = null;
                nextEnforcementAt = 0f;
                if (!active)
                {
                    return;
                }
                int observed;
                if (TryReadLocked(out observed))
                {
                    intervalAtSessionStart = observed;
                }
                EnforceLocked();
            }
        }

        /// <summary>
        /// Re-asserts 0 at most once per <see cref="EnforcementPeriodSeconds"/>.
        /// Returns the interval that was overwritten, or null when nothing changed.
        /// </summary>
        internal static int? Tick(float unscaledTime)
        {
            lock (Sync)
            {
                if (!active || unscaledTime < nextEnforcementAt)
                {
                    return null;
                }
                nextEnforcementAt = unscaledTime + EnforcementPeriodSeconds;
                return EnforceLocked();
            }
        }

        /// <summary>Re-asserts 0 now, e.g. just before a save is serialized.</summary>
        internal static int? EnforceNow()
        {
            lock (Sync)
            {
                return active ? EnforceLocked() : null;
            }
        }

        /// <summary>
        /// GameClock.DoAutoSave prefix decision.  True means the autosave must not run.
        /// </summary>
        internal static bool ShouldSuppressAutosave()
        {
            lock (Sync)
            {
                if (!active)
                {
                    return false;
                }
                suppressedAutosaveCount++;
                EnforceLocked();
                return true;
            }
        }

        internal static Dictionary<string, object> Describe()
        {
            lock (Sync)
            {
                int observed;
                bool readable = TryReadLocked(out observed);
                string status;
                if (!active)
                {
                    status = StatusInactive;
                }
                else if (!readable)
                {
                    status = StatusSaveUnavailable;
                }
                else if (observed != EnforcedInterval)
                {
                    status = StatusViolated;
                }
                else if (!suppressionPatchInstalled)
                {
                    status = StatusPatchMissing;
                }
                else
                {
                    status = StatusEnforced;
                }

                return new Dictionary<string, object>
                {
                    { "status", status },
                    { "guard_active", active },
                    { "activation_reason", activationReason },
                    { "cycle_interval", readable ? (object)observed : null },
                    { "interval_at_session_start", intervalAtSessionStart.HasValue ? (object)intervalAtSessionStart.Value : null },
                    { "enforced_interval", active ? (object)EnforcedInterval : null },
                    { "enforcement_count", enforcementCount },
                    { "suppressed_autosave_count", suppressedAutosaveCount },
                    { "suppression_patch_installed", suppressionPatchInstalled },
                    { "source", "SaveGame.Instance.AutoSaveCycleInterval" },
                    { "failure", lastFailure }
                };
            }
        }

        private static int? EnforceLocked()
        {
            int observed;
            if (!TryReadLocked(out observed))
            {
                return null;
            }
            if (observed == EnforcedInterval)
            {
                return null;
            }
            try
            {
                SaveGame.Instance.AutoSaveCycleInterval = EnforcedInterval;
                enforcementCount++;
                lastFailure = null;
                return observed;
            }
            catch (Exception ex)
            {
                lastFailure = "write_failed:" + ex.GetType().Name;
                return null;
            }
        }

        private static bool TryReadLocked(out int interval)
        {
            interval = 0;
            try
            {
                // Reference comparison on purpose: Unity's overloaded == needs the
                // native engine, and DestroyInstance clears the static on unload.
                SaveGame instance = SaveGame.Instance;
                if (ReferenceEquals(instance, null))
                {
                    return false;
                }
                interval = instance.AutoSaveCycleInterval;
                return true;
            }
            catch (Exception ex)
            {
                lastFailure = "read_failed:" + ex.GetType().Name;
                return false;
            }
        }
    }
}
