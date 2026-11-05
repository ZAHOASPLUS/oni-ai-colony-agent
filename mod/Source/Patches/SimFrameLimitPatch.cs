using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using OniAiProbeMod.Api;
using UnityEngine;

namespace OniAiProbeMod.Patches
{
    /// <summary>
    /// Game.SimEveryTick takes at most 0.2 game seconds from a frame, so the real
    /// speed is min(timeScale, 0.2 x FPS) however high timeScale goes.  Up to the
    /// game's own debug speed of 10x the limit stays as it is; above that it grows
    /// with timeScale, and the game's loop runs several sim ticks in one frame.
    /// </summary>
    internal static class SimFrameLimitPatch
    {
        private const float GameFrameLimit = 0.2f;
        private const float HighestUnchangedTimeScale = 10f;

        // Dupe brains update once per frame, so frames stay this frequent for as
        // long as the CPU keeps up.
        private const float FramesPerSecondKept = 20f;

        private static bool limitReplaced;

        internal static bool IsInstalled { get; private set; }

        internal static void Install(Harmony harmony)
        {
            try
            {
                MethodInfo target = AccessTools.Method(typeof(Game), "SimEveryTick", new[] { typeof(float) });
                if (target == null)
                {
                    ApiLog.Error("patch_sim_frame_limit", "Game.SimEveryTick(float) was not found.");
                    return;
                }

                harmony.Patch(
                    target,
                    transpiler: new HarmonyMethod(AccessTools.Method(typeof(SimFrameLimitPatch), nameof(UseFrameLimit))));
                IsInstalled = limitReplaced;
                if (!IsInstalled)
                {
                    ApiLog.Error("patch_sim_frame_limit", "The 0.2 limit was not found in Game.SimEveryTick.");
                }
            }
            catch (Exception ex)
            {
                ApiLog.Error("patch_sim_frame_limit", ex);
            }
        }

        internal static float FrameLimit()
        {
            float timeScale = Time.timeScale;
            return timeScale > HighestUnchangedTimeScale ? timeScale / FramesPerSecondKept : GameFrameLimit;
        }

        private static IEnumerable<CodeInstruction> UseFrameLimit(IEnumerable<CodeInstruction> instructions)
        {
            List<CodeInstruction> code = new List<CodeInstruction>(instructions);
            MethodInfo min = AccessTools.Method(typeof(Mathf), nameof(Mathf.Min), new[] { typeof(float), typeof(float) });
            for (int i = 0; i + 1 < code.Count; i++)
            {
                // The method's other 0.2 is the length of a sim tick, which must stay.
                if (code[i].opcode == OpCodes.Ldc_R4 && (float)code[i].operand == GameFrameLimit && code[i + 1].Calls(min))
                {
                    code[i].opcode = OpCodes.Call;
                    code[i].operand = AccessTools.Method(typeof(SimFrameLimitPatch), nameof(FrameLimit));
                    limitReplaced = true;
                    break;
                }
            }
            return code;
        }
    }
}
