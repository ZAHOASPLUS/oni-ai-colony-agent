using System.Collections.Generic;
using HarmonyLib;
using KMod;
using OniAiProbeMod.Api;
using OniAiProbeMod.Patches;
using OniAiProbeMod.Probe;
using UnityEngine;

namespace OniAiProbeMod
{
    public sealed class Mod : UserMod2
    {
        internal const string LogPrefix = "[ONI AI]";
        internal const string ModVersion = "1.0.0";

        public override void OnLoad(Harmony harmony)
        {
            base.OnLoad(harmony);
            ApiLog.Initialize();
            ApiMode.Initialize(ApiLog.BridgeDirectory);
            if (!ApiMode.Enabled)
            {
                // Without an autoload request the Mod changes nothing: normal play.
                return;
            }

            // A test process must never run ONI's autosave rotation, which deletes
            // the user's own colony autosaves.
            AutosaveGuard.Activate("api_mode");
            // A timelapse screenshot queued for a world that is no longer loaded
            // wedges the simulation clock.
            TimelapseGuard.Activate("api_mode");
            LifecyclePatches.Install(harmony);
            SimFrameLimitPatch.Install(harmony);
            ApiHost.Launch(ApiLog.BridgeDirectory);
            Debug.Log(LogPrefix + " Tool API mode");
        }
    }
}
