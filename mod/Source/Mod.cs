using System.Collections.Generic;
using HarmonyLib;
using KMod;
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
            if (StartupBinding.WasAccepted)
            {
                // P0-1: a validation process must never run ONI's autosave
                // rotation, which deletes the user's own colony autosaves.
                AutosaveGuard.Activate("startup_binding_accepted");
                Debug.Log(LogPrefix + " Autosave disabled for this validation process");
            }
            ProbeSession.InitializeProcess();
            ProbeWriter.WriteEvent("probe_loaded", new Dictionary<string, object>
            {
                { "mode", "observer_default" }
            });

            LifecyclePatches.Install(harmony);
            base.OnLoad(harmony);
        }
    }
}
