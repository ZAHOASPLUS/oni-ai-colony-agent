using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Suit and Oxygen Mask docks.  A new dock is unconfigured and fetches
    /// nothing; its side screen's two buttons call ConfigRequestSuit ("request a
    /// suit") and ConfigNoSuit ("no suit"), checked in the game's IL, 2026-11-06.
    /// </summary>
    internal static class ApiSuits
    {
        internal static void Describe(GameObject building, Dictionary<string, object> entry)
        {
            SuitLocker locker = building.GetComponent<SuitLocker>();
            if (locker == null)
            {
                return;
            }
            entry["suit_configured"] = locker.smi.sm.isConfigured.Get(locker.smi);
            entry["suit_requested"] = locker.smi.sm.isWaitingForSuit.Get(locker.smi);
            KPrefabID stored = locker.GetStoredOutfit();
            entry["stored_suit"] = stored == null ? null : stored.PrefabTag.Name;
            if (stored != null)
            {
                entry["suit_oxygen_pct"] = ApiValues.Number(locker.OxygenAvailable * 100f, 0);
                entry["suit_fully_charged"] = locker.IsSuitFullyCharged();
            }
        }

        /// <summary>request_suit true: the dock asks for a suit and keeps one; false: it holds none.</summary>
        internal static bool Configure(GameObject building, JObject args, Dictionary<string, object> applied)
        {
            JToken value = args["request_suit"];
            if (value == null)
            {
                return false;
            }
            if (value.Type != JTokenType.Boolean)
            {
                throw new ApiException("invalid_argument", "request_suit must be true or false.");
            }
            SuitLocker locker = building.GetComponent<SuitLocker>();
            if (locker == null || building.GetComponent<BuildingComplete>() == null)
            {
                throw new ApiException("unsupported_setting", "request_suit applies to a finished suit or mask dock.");
            }
            if (value.Value<bool>())
            {
                locker.ConfigRequestSuit();
            }
            else
            {
                locker.ConfigNoSuit();
            }
            Describe(building, applied);
            return true;
        }
    }
}
