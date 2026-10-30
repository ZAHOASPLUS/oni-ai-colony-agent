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
        internal const string LogPrefix = "[ONI AI Probe]";
        internal const string ModVersion = "0.7.0";

        public override void OnLoad(Harmony harmony)
        {
            Debug.Log(LogPrefix + " Loading observer-default probe V2");

            ProbeWriter.Initialize();
            StartupBinding.Initialize(ProbeWriter.BridgeDirectory);
            ApiMode.Initialize(ProbeWriter.BridgeDirectory);
            if (StartupBinding.WasAccepted || ApiMode.Enabled)
            {
                string reason = ApiMode.Enabled ? "api_mode" : "startup_binding_accepted";
                // P0-1: a validation process must never run ONI's autosave
                // rotation, which deletes the user's own colony autosaves.
                AutosaveGuard.Activate(reason);
                Debug.Log(LogPrefix + " Autosave disabled for this validation process");
                // A timelapse screenshot queued for a world that is no longer
                // loaded wedges the simulation clock, which an autonomous run
                // depends on.
                TimelapseGuard.Activate(reason);
                Debug.Log(LogPrefix + " Timelapse screenshot queue disabled for this validation process");
            }
            ProbeSession.InitializeProcess();
            ProbeWriter.WriteEvent("probe_loaded", new Dictionary<string, object>
            {
                { "mode", "observer_default" }
            });

            LifecyclePatches.Install(harmony);
            base.OnLoad(harmony);
            if (ApiMode.Enabled)
            {
                ApiHost.Launch(ProbeWriter.BridgeDirectory);
            }
        }
    }
}
