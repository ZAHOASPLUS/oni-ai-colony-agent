using System.Collections.Generic;
using Klei.AI;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Critters a player can see, the game's Wrangle order, and the critter limit
    /// a Critter Drop-Off sets in its side screen.
    /// </summary>
    internal static class ApiCritters
    {
        internal static List<Dictionary<string, object>> Find(WorldContainer world, string query)
        {
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            foreach (Brain brain in Components.Brains.Items)
            {
                if (brain == null || !(brain is CreatureBrain) || brain.gameObject.GetMyWorldId() != world.id)
                {
                    continue;
                }
                int cell = Grid.PosToCell(brain.gameObject);
                if (!Grid.IsValidCell(cell) || !Grid.IsVisible(cell))
                {
                    continue;
                }
                KPrefabID prefab = brain.GetComponent<KPrefabID>();
                if (prefab == null || prefab.HasTag(GameTags.Dead))
                {
                    continue;
                }
                if (query != null && !string.Equals(prefab.PrefabTag.Name, query, System.StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                found.Add(Describe(brain.gameObject, cell));
            }
            return found;
        }

        private static Dictionary<string, object> Describe(GameObject go, int cell)
        {
            KPrefabID prefab = go.GetComponent<KPrefabID>();
            Dictionary<string, object> entry = ApiValues.Xy(cell);
            entry["id"] = prefab.InstanceID;
            entry["critter"] = prefab.PrefabTag.Name;
            entry["name"] = go.GetProperName();
            entry["wild"] = prefab.HasTag(GameTags.Creatures.Wild);
            AmountInstance age = Db.Get().Amounts.Age.Lookup(go);
            if (age != null)
            {
                entry["age_cycles"] = ApiValues.Number(age.value, 1);
                entry["max_age_cycles"] = ApiValues.Number(age.GetMax(), 0);
            }
            AmountInstance wildness = Db.Get().Amounts.Wildness.Lookup(go);
            if (wildness != null)
            {
                entry["wildness_pct"] = ApiValues.Number(wildness.value, 0);
            }
            AttributeInstance happiness = Db.Get().CritterAttributes.Happiness.Lookup(go);
            if (happiness != null)
            {
                entry["happiness"] = ApiValues.Number(happiness.GetTotalValue(), 1);
            }
            Capturable capturable = go.GetComponent<Capturable>();
            entry["capturable"] = capturable != null && capturable.IsCapturable();
            entry["marked_for_capture"] = capturable != null && capturable.IsMarkedForCapture;
            Room room = Game.Instance.roomProber.GetRoomOfGameObject(go);
            entry["room"] = room?.roomType?.Id;
            return entry;
        }

        internal static void DescribeBuilding(GameObject building, Dictionary<string, object> entry)
        {
            BaggableCritterCapacityTracker tracker = building.GetComponent<BaggableCritterCapacityTracker>();
            if (tracker != null)
            {
                entry["critter_limit"] = tracker.creatureLimit;
                entry["critters_in_room"] = tracker.storedCreatureCount;
            }
        }

        /// <summary>
        /// capture: true marks a critter for wrangling (Capturable.MarkForCapture,
        /// what the Wrangle button does); false clears the mark.  The errand is the
        /// game's own and needs a duplicant with the ranching skill.
        /// critter_limit: a Critter Drop-Off's limit for its room.
        /// </summary>
        internal static bool Configure(GameObject target, JObject args, Dictionary<string, object> applied)
        {
            bool any = false;
            JToken capture = args["capture"];
            if (capture != null)
            {
                if (capture.Type != JTokenType.Boolean)
                {
                    throw new ApiException("invalid_argument", "capture must be true or false.");
                }
                Capturable capturable = target.GetComponent<Capturable>();
                if (capturable == null)
                {
                    throw new ApiException("unsupported_setting", "Only a critter takes capture; the id comes from find kind critter.");
                }
                bool wanted = capture.Value<bool>();
                if (wanted && !capturable.IsCapturable())
                {
                    throw new ApiException(
                        "cannot_capture",
                        target.GetProperName() + " cannot be wrangled now.",
                        "Some critters cannot be wrangled at all, and none while being carried or already in a bag.");
                }
                if (capturable.IsMarkedForCapture != wanted)
                {
                    capturable.MarkForCapture(wanted);
                }
                applied["capture"] = capturable.IsMarkedForCapture ? "marked" : "not_marked";
                any = true;
            }
            JToken limit = args["critter_limit"];
            if (limit != null)
            {
                BaggableCritterCapacityTracker tracker = target.GetComponent<BaggableCritterCapacityTracker>();
                if (tracker == null)
                {
                    throw new ApiException("unsupported_setting", "critter_limit applies to a Critter Drop-Off.");
                }
                if (limit.Type != JTokenType.Integer || limit.Value<int>() < 0 || limit.Value<int>() > 20)
                {
                    throw new ApiException("invalid_argument", "critter_limit must be an integer from 0 to 20.");
                }
                // The capacity side screen writes this same interface property.
                ((IUserControlledCapacity)tracker).UserMaxCapacity = limit.Value<int>();
                applied["critter_limit"] = tracker.creatureLimit;
                any = true;
            }
            return any;
        }
    }
}
