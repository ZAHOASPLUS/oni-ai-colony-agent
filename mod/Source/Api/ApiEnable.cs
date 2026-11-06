using System.Collections.Generic;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The Enable/Disable building button.  The button (BuildingEnabledButton.
    /// OnMenuToggle, checked in the game's IL, 2026-11-06) does not flip the
    /// building: it queues a Toggle errand that a duplicant walks over to do, and
    /// pressing it again while queued cancels the errand.
    /// </summary>
    internal static class ApiEnable
    {
        private static readonly AccessTools.FieldRef<BuildingEnabledButton, Toggleable> ToggleableOf =
            AccessTools.FieldRefAccess<BuildingEnabledButton, Toggleable>("Toggleable");
        private static readonly AccessTools.FieldRef<BuildingEnabledButton, int> ToggleIdxOf =
            AccessTools.FieldRefAccess<BuildingEnabledButton, int>("ToggleIdx");
        private static readonly System.Reflection.MethodInfo MenuToggle =
            AccessTools.Method(typeof(BuildingEnabledButton), "OnMenuToggle");

        private static bool Queued(BuildingEnabledButton button)
        {
            Toggleable toggleable = ToggleableOf(button);
            return toggleable != null && toggleable.IsToggleQueued(ToggleIdxOf(button));
        }

        internal static void Describe(GameObject building, Dictionary<string, object> entry)
        {
            BuildingEnabledButton button = building.GetComponent<BuildingEnabledButton>();
            if (button == null)
            {
                return;
            }
            entry["enabled"] = button.IsEnabled;
            entry["enable_toggle_queued"] = Queued(button);
        }

        /// <summary>
        /// enabled true or false: queue the duplicant errand that switches the
        /// building to that state, or cancel a queued errand that would switch it
        /// away from it.
        /// </summary>
        internal static bool Configure(GameObject building, JObject args, Dictionary<string, object> applied)
        {
            JToken value = args["enabled"];
            if (value == null)
            {
                return false;
            }
            if (value.Type != JTokenType.Boolean)
            {
                throw new ApiException("invalid_argument", "enabled must be true or false.");
            }
            BuildingEnabledButton button = building.GetComponent<BuildingEnabledButton>();
            if (button == null || building.GetComponent<BuildingComplete>() == null)
            {
                throw new ApiException("unsupported_setting", "enabled applies to a finished building with an Enable/Disable button.");
            }
            bool wanted = value.Value<bool>();
            bool queued = Queued(button);
            string result;
            if (button.IsEnabled == wanted)
            {
                if (queued)
                {
                    MenuToggle.Invoke(button, null);
                    result = "toggle_cancelled";
                }
                else
                {
                    result = wanted ? "already_enabled" : "already_disabled";
                }
            }
            else if (queued)
            {
                result = "toggle_already_queued";
            }
            else
            {
                MenuToggle.Invoke(button, null);
                result = "toggle_queued";
            }
            applied["enabled"] = result;
            Describe(building, applied);
            return true;
        }
    }
}
