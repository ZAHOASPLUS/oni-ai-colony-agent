using System;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;

namespace OniAiProbeMod.Agent
{
    /// <summary>Main-thread pause ownership; an external/player pause always wins.</summary>
    internal static class AgentPauseControl
    {
        private static bool agentCall;
        private static bool agentOwnsPause;
        private static bool hooksInstalled;
        private static DateTime pauseStarted;

        internal static void Install(Harmony harmony)
        {
            var pause = AccessTools.Method(typeof(SpeedControlScreen), "Pause", new[] { typeof(bool), typeof(bool) });
            var unpause = AccessTools.Method(typeof(SpeedControlScreen), "Unpause", new[] { typeof(bool) });
            if (pause == null || unpause == null) return; // Do not claim ownership protection without both hooks.
            harmony.Patch(pause, prefix: new HarmonyMethod(typeof(AgentPauseControl), nameof(OnPause)));
            harmony.Patch(unpause, prefix: new HarmonyMethod(typeof(AgentPauseControl), nameof(OnUnpause)));
            hooksInstalled = true;
        }

        private static void OnPause()
        {
            if (!agentCall) agentOwnsPause = false;
        }
        private static void OnUnpause()
        {
            if (!agentCall) agentOwnsPause = false;
        }
        internal static void Reset() { agentOwnsPause = false; }
        internal static bool NeedsResumeReminder => agentOwnsPause && DateTime.UtcNow - pauseStarted > TimeSpan.FromMinutes(2);

        internal static JObject Execute(JObject args, string policy, string session)
        {
            if (!hooksInstalled)
                return ApiResult.Failure("pause_hooks_unverified", "Pause ownership hooks are unavailable; Agent speed changes are disabled.");
            int speed = (int)args["speed"];
            if (speed == 0 && policy == "never_pause")
                return ApiResult.Failure("pause_policy", "The player selected never_pause.");
            if (speed == 0 && policy == "allow_pause_require_resume")
                return ApiResult.Failure("resume_condition_unavailable", "This development build does not yet validate a resume condition; use agent_decides or never_pause.");
            var control = SpeedControlScreen.Instance;
            if (control == null) return ApiResult.Failure("game_not_loaded", "No speed controller is available.");
            if (speed != 0 && control.IsPaused && !agentOwnsPause)
                return ApiResult.Failure("player_pause", "A player or game dialog owns this pause; the Agent cannot resume it.");
            bool wasPaused = control.IsPaused;
            try
            {
                agentCall = true;
                JObject outcome = ApiTools.Invoke("set_speed", args, "agent", session);
                if ((bool?)outcome["ok"] == true)
                {
                    if (speed == 0 && !wasPaused)
                    {
                        agentOwnsPause = true;
                        pauseStarted = DateTime.UtcNow;
                    }
                    if (speed != 0) agentOwnsPause = false;
                }
                return outcome;
            }
            finally { agentCall = false; }
        }
    }
}
