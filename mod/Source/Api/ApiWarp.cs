using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The Teleporter Transmitter (WarpPortal), with the guards of its side
    /// screen (checked in the game's IL, 2026-11-06): the start button calls
    /// StartWarpSequence only when ReadyToWarp, and cancel calls
    /// CancelAssignment.  Who goes is set with configure assigned_duplicant,
    /// which gives them the game's own walk-in errand.
    /// </summary>
    internal static class ApiWarp
    {
        internal static void Describe(GameObject building, Dictionary<string, object> entry)
        {
            WarpPortal portal = building.GetComponent<WarpPortal>();
            if (portal == null)
            {
                return;
            }
            entry["warp_ready"] = portal.ReadyToWarp;
            entry["warping"] = portal.IsWorking;
            entry["warp_recharging"] = portal.IsConsumed;
        }

        internal static bool Configure(GameObject building, JObject args, Dictionary<string, object> applied)
        {
            string action = ApiArgs.Text(args, "warp_action");
            if (action == null)
            {
                return false;
            }
            WarpPortal portal = building.GetComponent<WarpPortal>();
            if (portal == null)
            {
                throw new ApiException("unsupported_setting", "warp_action applies to a Teleporter Transmitter (WarpPortal).");
            }
            switch (action)
            {
                case "start":
                    if (!portal.ReadyToWarp)
                    {
                        throw new ApiException(
                            "not_ready",
                            "Nobody is waiting inside the transmitter.",
                            "Assign a duplicant with assigned_duplicant, wait until inspect shows warp_ready, then start.");
                    }
                    portal.StartWarpSequence();
                    break;
                case "cancel":
                    if (portal.IsWorking || portal.IsConsumed)
                    {
                        throw new ApiException("cannot_cancel", "The transmitter is warping or recharging; there is nothing to cancel.");
                    }
                    portal.CancelAssignment();
                    break;
                default:
                    throw new ApiException("invalid_argument", "warp_action must be start or cancel.");
            }
            applied["warp_action"] = action;
            Describe(building, applied);
            return true;
        }
    }
}
