using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Keeps ONI's timelapse screenshotting from running during a validation
    /// process, so the game cannot wedge or crash on a world that is no longer
    /// loaded.
    ///
    /// Observed 2026-10-25 (rounds 13, 15, 17): the game froze with
    /// <c>game_time_cycles</c> constant for many minutes while Player.log repeated
    ///
    ///     Timelapse.OnNewDay but worldsToScreenshot is not empty
    ///     Saving screenshot to ...\RetiredColonies\&lt;colony&gt;\00000\...
    ///
    /// <c>Timelapser.OnNewDay</c> appends world ids to <c>worldsToScreenshot</c> and
    /// the <c>Render</c> coroutine walks that list by index.  An id whose world is
    /// gone never completes, so the queue never drains and the simulation stops
    /// advancing.  An autonomous run depends on the cycle clock advancing, so the
    /// hang is fatal to it even though it is not caused by it.
    ///
    /// The guard is deliberately NON-DESTRUCTIVE.  An earlier version cleared
    /// <c>worldsToScreenshot</c> while <c>Render</c> was iterating it, and the
    /// coroutine then read past the end of the list:
    ///
    ///     ArgumentOutOfRangeException: Index was out of range.
    ///       at System.Collections.Generic.List`1[T].get_Item (Int32 index)
    ///       at Timelapser+&lt;Render&gt;d__29.MoveNext ()
    ///
    /// which ended the game session (round 19).  The guard therefore only
    /// (1) forces <c>screenshotToday</c> true so <c>OnNewDay</c> does not schedule
    /// a screenshot for the day and (2) suppresses <c>Timelapser.Update</c> by
    /// prefix, so no timelapse render is ever started.  It never mutates the list.
    ///
    /// It is armed only when a fresh startup binding was accepted, exactly like
    /// <see cref="AutosaveGuard"/>, so normal play is never changed.
    /// </summary>
    internal static class TimelapseGuard
    {
        internal const float EnforcementPeriodSeconds = 2f;

        internal const string StatusInactive = "inactive";
        internal const string StatusEnforced = "enforced";
        internal const string StatusTimelapserUnavailable = "timelapser_unavailable";
        internal const string StatusFieldMissing = "field_missing";
        internal const string StatusPatchMissing = "suppression_patch_missing";
        internal const string StatusFailed = "failed";

        private const string TimelapserTypeName = "Timelapser";
        private const string WorldsFieldName = "worldsToScreenshot";
        private const string ScreenshotTodayFieldName = "screenshotToday";

        private static readonly object Sync = new object();
        private static bool active;
        private static string activationReason;
        private static bool fieldsResolved;
        private static FieldInfo worldsField;
        private static FieldInfo screenshotTodayField;
        private static Component cachedTimelapser;
        private static bool suppressionPatchInstalled;
        private static long enforcementCount;
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

        /// <summary>
        /// Re-asserts <c>screenshotToday</c> at most once per
        /// <see cref="EnforcementPeriodSeconds"/>.  Returns true when the field was
        /// written, or null when nothing was done.
        /// </summary>
        internal static bool? Tick(float unscaledTime)
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

        /// <summary>
        /// Timelapser.Update prefix decision.  True means the update must not run,
        /// so no timelapse render is started for this process.
        /// </summary>
        internal static bool ShouldSuppressUpdate()
        {
            lock (Sync)
            {
                return active;
            }
        }

        internal static void MarkSuppressionPatchInstalled()
        {
            lock (Sync)
            {
                suppressionPatchInstalled = true;
            }
        }

        internal static Dictionary<string, object> Describe()
        {
            lock (Sync)
            {
                string status;
                if (!active)
                {
                    status = StatusInactive;
                }
                else if (lastFailure != null)
                {
                    status = StatusFailed;
                }
                else if (!fieldsResolved)
                {
                    status = StatusFieldMissing;
                }
                else if (cachedTimelapser == null)
                {
                    status = StatusTimelapserUnavailable;
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
                    { "enforcement_count", enforcementCount },
                    { "fields_resolved", fieldsResolved },
                    { "suppression_patch_installed", suppressionPatchInstalled },
                    { "source", "Timelapser.screenshotToday" },
                    { "failure", lastFailure }
                };
            }
        }

        private static bool? EnforceLocked()
        {
            Component timelapser = FindTimelapserLocked();
            if (timelapser == null)
            {
                return null;
            }
            if (!ResolveFieldsLocked(timelapser))
            {
                return null;
            }

            try
            {
                // Only ever write a flag.  Clearing worldsToScreenshot here would
                // pull the list out from under a Render coroutine that is iterating
                // it by index, which threw ArgumentOutOfRangeException and ended
                // the session (round 19).
                screenshotTodayField.SetValue(timelapser, true);
                lastFailure = null;
            }
            catch (Exception ex)
            {
                // A failed write must not be silent, and the cached component is
                // dropped so the next tick re-resolves a live instance.
                lastFailure = "write_failed:" + ex.GetType().Name;
                cachedTimelapser = null;
                fieldsResolved = false;
                return null;
            }

            enforcementCount++;
            return true;
        }

        private static Component FindTimelapserLocked()
        {
            if (cachedTimelapser != null)
            {
                return cachedTimelapser;
            }
            try
            {
                Type timelapserType = ResolveTimelapserTypeLocked();
                if (timelapserType == null)
                {
                    lastFailure = "type_missing:" + TimelapserTypeName;
                    return null;
                }
#pragma warning disable CS0618 // The sorted result is not needed, but this call is the one proven live.
                UnityEngine.Object[] found = UnityEngine.Object.FindObjectsOfType(timelapserType);
#pragma warning restore CS0618
                if (found != null && found.Length > 0 && found[0] is Component component)
                {
                    cachedTimelapser = component;
                    lastFailure = null;
                }
                else
                {
                    lastFailure = null;
                }
            }
            catch (Exception ex)
            {
                lastFailure = "find_failed:" + ex.GetType().Name;
            }
            return cachedTimelapser;
        }

        private static Type timelapserType;

        private static Type ResolveTimelapserTypeLocked()
        {
            if (timelapserType != null)
            {
                return timelapserType;
            }
            // Assembly-CSharp is already loaded: this mod is compiled against it.
            Type found = Type.GetType(TimelapserTypeName + ", Assembly-CSharp", throwOnError: false);
            if (found != null)
            {
                timelapserType = found;
            }
            return timelapserType;
        }

        private static bool ResolveFieldsLocked(Component timelapser)
        {
            if (fieldsResolved)
            {
                return worldsField != null && screenshotTodayField != null;
            }
            const BindingFlags Flags =
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            Type type = timelapser.GetType();
            worldsField = type.GetField(WorldsFieldName, Flags);
            screenshotTodayField = type.GetField(ScreenshotTodayFieldName, Flags);
            fieldsResolved = worldsField != null && screenshotTodayField != null;
            if (!fieldsResolved)
            {
                lastFailure = "field_missing:"
                    + (worldsField == null ? WorldsFieldName : ScreenshotTodayFieldName);
            }
            return fieldsResolved;
        }
    }
}
