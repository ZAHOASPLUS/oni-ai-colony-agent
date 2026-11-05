using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Living duplicants by the id get_status gives them.  A name is not enough:
    /// the printing pod offers names the colony already has.
    /// </summary>
    internal static class ApiDuplicants
    {
        /// <summary>The KPrefabID instance id, as for buildings; the save keeps it.</summary>
        internal static int Id(MinionIdentity identity)
        {
            return identity.GetComponent<KPrefabID>().InstanceID;
        }

        internal static MinionIdentity Read(JObject args, string name)
        {
            return Find(args[name], name);
        }

        /// <summary>The living duplicant an argument means: its id, or a name no other one has.</summary>
        internal static MinionIdentity Find(JToken value, string argument)
        {
            List<MinionIdentity> living = Components.LiveMinionIdentities.Items
                .Where(identity => identity != null)
                .ToList();
            return Pick(living, value, argument, Id, identity => identity.GetProperName());
        }

        // Free of game types, so the offline harness can check it.
        internal static T Pick<T>(IList<T> living, JToken value, string argument, Func<T, int> idOf, Func<T, string> nameOf)
            where T : class
        {
            if (value != null && value.Type == JTokenType.Integer)
            {
                long id = value.Value<long>();
                T match = living.FirstOrDefault(candidate => idOf(candidate) == id);
                if (match == null)
                {
                    throw new ApiException("unknown_duplicant", "No living duplicant has id " + id + ".", "get_status lists them.");
                }
                return match;
            }
            if (value == null || value.Type != JTokenType.String)
            {
                throw new ApiException("invalid_argument", argument + " must be a duplicant's id from get_status or its name.");
            }

            // Digits are an id, but a duplicant may be named so: then both are candidates.
            string text = value.Value<string>();
            int number;
            bool numeric = int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out number);
            List<T> matches = living
                .Where(candidate => nameOf(candidate) == text || (numeric && idOf(candidate) == number))
                .ToList();
            if (matches.Count == 0)
            {
                throw new ApiException("unknown_duplicant", "No living duplicant has the id or name " + text + ".", "get_status lists them.");
            }
            if (matches.Count > 1)
            {
                List<int> ids = matches.Select(idOf).ToList();
                throw new ApiException(
                    "ambiguous_duplicant",
                    ids.Count + " living duplicants match " + text + ": ids " + string.Join(", ", ids) + ".",
                    "Give " + argument + " as one of these ids; get_status shows where each one is and what it is doing.",
                    new JObject { ["candidate_ids"] = new JArray(ids) });
            }
            return matches[0];
        }
    }
}
