using System.Globalization;
using System.Reflection;

namespace OniAiProbeMod.Commands
{
    /// <summary>
    /// Mirrors SpeedControlScreen's private pause reference count.
    ///
    /// Code review 2026-09-26 (P2): ONI's pause is reference counted.  Every
    /// pausing modal (KModalScreen with pause=true, KModalButtonMenu such as the
    /// pause menu, the save/load screens, story sequences) calls Pause() when it
    /// opens and Unpause() when it closes.  The executor's set_paused=false called
    /// Unpause() once whatever held the pause, so it either took a modal's
    /// reference (the game ran under an open menu) or released only one of
    /// several holders and then failed verification as outcome_unknown.  The
    /// executor now refuses the unpause before dispatch unless the pause is held
    /// exactly once and no pausing modal is open.
    ///
    /// The count is observed by Harmony prefix/postfix patches on Pause/Unpause
    /// (installed only for a validation process), never by reflection from the
    /// executor.  A change whose postfix did not run leaves the count unknown.
    /// </summary>
    internal static class PauseLedger
    {
        private static readonly object Sync = new object();
        // KModalScreen and KModalButtonMenu set their private `shown` exactly when
        // OnShow(true) calls Pause() and clear it when OnShow(false) calls Unpause().
        private static readonly FieldInfo ModalScreenShown =
            typeof(KModalScreen).GetField("shown", BindingFlags.Instance | BindingFlags.NonPublic);
        private static readonly FieldInfo ModalMenuShown =
            typeof(KModalButtonMenu).GetField("shown", BindingFlags.Instance | BindingFlags.NonPublic);
        private static bool tracking;
        private static object observedScreen;
        private static int observedCount = -1;

        internal static bool IsTracking
        {
            get
            {
                lock (Sync)
                {
                    return tracking;
                }
            }
        }

        internal static void MarkTracking()
        {
            lock (Sync)
            {
                tracking = true;
            }
        }

        /// <summary>Pause/Unpause prefix: the count is unknown until the postfix runs.</summary>
        internal static void BeginChange(object screen)
        {
            lock (Sync)
            {
                observedScreen = screen;
                observedCount = -1;
            }
        }

        /// <summary>Pause/Unpause postfix with the screen's count after the call.</summary>
        internal static void Observe(object screen, int pauseCount)
        {
            lock (Sync)
            {
                observedScreen = screen;
                observedCount = pauseCount < 0 ? -1 : pauseCount;
            }
        }

        internal static bool TryGetCount(object screen, out int pauseCount)
        {
            lock (Sync)
            {
                pauseCount = observedCount;
                return screen != null && ReferenceEquals(screen, observedScreen) && observedCount >= 0;
            }
        }

        /// <summary>
        /// Null when a single Unpause() releases the game's only pause reference;
        /// otherwise the reason the executor must not unpause.
        /// </summary>
        internal static string CountRefusal(bool isTracking, bool countKnown, int pauseCount)
        {
            if (!isTracking)
            {
                return "pause references are not tracked (SpeedControlScreen patch unavailable)";
            }
            if (!countKnown)
            {
                return "the pause reference count of the current SpeedControlScreen is unknown";
            }
            if (pauseCount != 1)
            {
                return "the pause is held " + pauseCount.ToString(CultureInfo.InvariantCulture) +
                    " times; one unpause would not release it and would take another holder's pause";
            }
            return null;
        }

        /// <summary>
        /// True while a modal holds the pause reference it took when shown.  A
        /// hidden screen can stay active in the hierarchy (the pause menu is
        /// created hidden and lives for the whole game), so being active is not
        /// the signal: the first live run (2026-09-27) refused every unpause with
        /// "1 pausing modal" while the pause was held exactly once.  An unreadable
        /// flag counts as holding the pause (fail closed).
        /// </summary>
        internal static bool ModalHoldsPause(object modal)
        {
            if (modal is KModalScreen screen)
            {
                return screen.pause && ReadShown(ModalScreenShown, screen);
            }
            if (modal is KModalButtonMenu menu)
            {
                return ReadShown(ModalMenuShown, menu);
            }
            return false;
        }

        private static bool ReadShown(FieldInfo field, object modal)
        {
            if (field == null || field.FieldType != typeof(bool))
            {
                return true;
            }
            try
            {
                return (bool)field.GetValue(modal);
            }
            catch
            {
                return true;
            }
        }

        /// <summary>Null when no pausing modal holds that single reference.</summary>
        internal static string ModalRefusal(int pausingModalCount)
        {
            return pausingModalCount > 0
                ? pausingModalCount.ToString(CultureInfo.InvariantCulture) +
                    " pausing modal screen(s) are open; unpausing would take their pause"
                : null;
        }
    }
}
