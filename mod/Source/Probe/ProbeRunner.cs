using System;
using System.Collections.Generic;
using System.Diagnostics;
using OniAiProbeMod.Commands;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    public sealed class ProbeRunner : MonoBehaviour
    {
        private const float SnapshotIntervalSeconds = 5f;
        private const string TrustedSessionStartReason = "save_loader_load_complete";
        private static GameObject runnerObject;
        private static bool actionExecutorStarted;
        private static bool validationControllerStarted;
        private static bool trustedSessionStarted;
        private float nextSnapshotAt;

        public static void EnsureStarted(string reason)
        {
            if (runnerObject != null)
            {
                return;
            }

            bool trustedReason = string.Equals(
                reason,
                TrustedSessionStartReason,
                StringComparison.Ordinal);
            // Before anything else can let the game clock run: the interval just
            // came back from the save, so force autosave off again (P0-1).
            AutosaveGuard.OnSessionStarted();
            ProbeSession.StartSaveSession();
            ProbeStateReader.ResetSession();
            if (trustedReason)
            {
                ProbeWriter.WriteEvent("session_started", new Dictionary<string, object>
                {
                    { "reason", reason }
                });
            }
            else
            {
                ProbeWriter.WriteEvent("session_start_unbound", new Dictionary<string, object>
                {
                    { "reason", reason },
                    { "controller_eligible", false }
                });
            }
            trustedSessionStarted = trustedReason;

            if (trustedSessionStarted && StartupBinding.IsSessionVerified && !ProbeWriter.HasSafetyFault)
            {
                ActionExecutor.BeginSession();
                actionExecutorStarted = true;
                ValidationCheckpointController.BeginSession();
                validationControllerStarted = true;
            }

            runnerObject = new GameObject("ONI AI Probe Runner");
            UnityEngine.Object.DontDestroyOnLoad(runnerObject);
            runnerObject.AddComponent<ProbeRunner>();

            ProbeWriter.WriteEvent("save_loaded", new Dictionary<string, object>
            {
                { "reason", reason }
            });
            // After save_loaded: startup evidence requires session_started and
            // save_loaded to be the session's first two records.
            if (AutosaveGuard.IsActive)
            {
                ProbeWriter.WriteEvent("autosave_guard", new Dictionary<string, object>
                {
                    { "autosave", AutosaveGuard.Describe() }
                });
            }
        }

        public static void Stop(string reason)
        {
            if (runnerObject == null)
            {
                return;
            }

            ProbeWriter.WriteEvent("save_unloaded", new Dictionary<string, object>
            {
                { "reason", reason }
            });
            ProbeWriter.WriteEvent("session_ended", new Dictionary<string, object>
            {
                { "reason", reason }
            });

            UnityEngine.Object.Destroy(runnerObject);
            runnerObject = null;
            StopControllers();
            trustedSessionStarted = false;
            ProbeSession.EndSaveSession();
        }

        private void Awake()
        {
            nextSnapshotAt = 0f;
        }

        private void Update()
        {
            int? overwrittenInterval = AutosaveGuard.Tick(Time.unscaledTime);
            if (overwrittenInterval.HasValue)
            {
                // Something (the options screen, another mod) turned autosave
                // back on during a validation session.
                ProbeWriter.WriteEvent("autosave_guard_enforced", new Dictionary<string, object>
                {
                    { "overwritten_interval", overwrittenInterval.Value },
                    { "autosave", AutosaveGuard.Describe() }
                });
            }

            // A timelapse screenshot queued for a world that is no longer loaded
            // never completes, and the stuck queue freezes the simulation clock
            // (2026-10-25 rounds 13, 15, 17).  Report only the transitions so the
            // event log stays small.
            bool? timelapseEnforced = TimelapseGuard.Tick(Time.unscaledTime);
            if (timelapseEnforced.HasValue && timelapseEnforced.Value)
            {
                ProbeWriter.WriteEvent("timelapse_guard_enforced", new Dictionary<string, object>
                {
                    { "timelapse", TimelapseGuard.Describe() }
                });
            }

            if (ProbeWriter.HasSafetyFault)
            {
                StopControllers();
            }

            if (trustedSessionStarted && StartupBinding.IsSessionVerified && !ProbeWriter.HasSafetyFault)
            {
                if (actionExecutorStarted)
                {
                    ActionExecutor.Update(Time.unscaledTime);
                }
                if (validationControllerStarted)
                {
                    ValidationCheckpointController.Update(Time.unscaledTime);
                }
            }

            if (Time.unscaledTime < nextSnapshotAt)
            {
                return;
            }

            nextSnapshotAt = Time.unscaledTime + SnapshotIntervalSeconds;
            try
            {
                Stopwatch stopwatch = Stopwatch.StartNew();
                Dictionary<string, object> snapshot = ProbeStateReader.ReadState();
                stopwatch.Stop();
                snapshot["sample"] = new Dictionary<string, object>
                {
                    { "interval_seconds", SnapshotIntervalSeconds },
                    { "duration_ms", Math.Round(stopwatch.Elapsed.TotalMilliseconds, 3) }
                };
                ProbeWriter.WriteRecord(snapshot);
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("state_snapshot", ex);
            }
        }

        private void OnDestroy()
        {
            if (runnerObject == gameObject)
            {
                runnerObject = null;
            }
        }

        private void OnApplicationQuit()
        {
            if (ProbeSession.IsSaveSessionActive)
            {
                ProbeWriter.WriteEvent("session_ended", new Dictionary<string, object>
                {
                    { "reason", "application_quit" }
                });
            }
            StopControllers();
            ProbeWriter.WriteEvent("probe_shutdown");
            ProbeWriter.Shutdown();
        }

        internal static void StopControllers()
        {
            if (actionExecutorStarted)
            {
                actionExecutorStarted = false;
                ActionExecutor.EndSession();
            }
            if (validationControllerStarted)
            {
                validationControllerStarted = false;
                ValidationCheckpointController.EndSession();
            }
        }
    }
}
