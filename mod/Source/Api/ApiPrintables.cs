using System.Collections.Generic;
using System.Linq;
using Klei.CustomSettings;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The printing pod's offer.  The game builds its offer inside the selection
    /// screen, so this builds the same mix without one: one or two care packages
    /// when they are enabled, duplicants for the rest.
    /// </summary>
    internal static class ApiPrintables
    {
        private const int OfferSize = 4;
        private const int CarePackageChancePercent = 70;

        private sealed class Offer
        {
            internal ITelepadDeliverable Deliverable;
            internal Dictionary<string, object> Description;
        }

        private static List<Offer> offers;

        internal static object Read(JObject args)
        {
            Immigration immigration = Immigration.Instance;
            if (!immigration.ImmigrantsAvailable)
            {
                offers = null;
                return new Dictionary<string, object>
                {
                    { "available", false },
                    { "next_in_seconds", ApiValues.Number(immigration.GetTimeRemaining(), 0) }
                };
            }
            if (offers == null)
            {
                offers = MakeOffers();
            }
            return new Dictionary<string, object>
            {
                { "available", true },
                { "options", offers.Select((offer, index) => Numbered(offer, index)).ToList() }
            };
        }

        internal static object Choose(JObject args)
        {
            RequireOffer();
            int index = ApiArgs.Int(args, "index", -1);
            if (index < 0 || index >= offers.Count)
            {
                throw new ApiException("invalid_argument", "index must be one of the options from get_printables.");
            }
            Offer chosen = offers[index];
            offers = null;
            Telepad().OnAcceptDelivery(chosen.Deliverable);
            return chosen.Description;
        }

        internal static object Reject(JObject args)
        {
            RequireOffer();
            offers = null;
            Telepad().RejectAll();
            return new Dictionary<string, object> { { "rejected", true } };
        }

        private static void RequireOffer()
        {
            if (!Immigration.Instance.ImmigrantsAvailable || offers == null)
            {
                throw new ApiException("no_offer", "There is no open offer.", "Call get_printables first.");
            }
        }

        private static Telepad Telepad()
        {
            Telepad telepad = Components.Telepads.Items.FirstOrDefault(item => item != null);
            if (telepad == null)
            {
                throw new ApiException("no_printing_pod", "This colony has no printing pod.");
            }
            return telepad;
        }

        private static List<Offer> MakeOffers()
        {
            int packages = 0;
            if (CustomGameSettings.Instance.GetCurrentQualitySetting(CustomGameSettingConfigs.CarePackages).id == "Enabled")
            {
                packages = Random.Range(0, 101) <= CarePackageChancePercent ? 1 : 2;
            }

            List<Offer> made = new List<Offer>();
            for (int index = 0; index < OfferSize - packages; index++)
            {
                MinionStartingStats stats = new MinionStartingStats(GameTags.Minions.Models.Standard, false);
                made.Add(new Offer { Deliverable = stats, Description = Describe(stats) });
            }
            for (int index = 0; index < packages; index++)
            {
                CarePackageInfo package = Immigration.Instance.RandomCarePackage();
                made.Add(new Offer { Deliverable = package, Description = Describe(package) });
            }
            return made;
        }

        private static Dictionary<string, object> Numbered(Offer offer, int index)
        {
            Dictionary<string, object> entry = new Dictionary<string, object>(offer.Description)
            {
                { "index", index }
            };
            return entry;
        }

        private static Dictionary<string, object> Describe(MinionStartingStats stats)
        {
            return new Dictionary<string, object>
            {
                { "kind", "duplicant" },
                { "name", stats.Name },
                { "traits", stats.Traits.Select(trait => trait.Id).ToList() },
                { "stress_reaction", stats.stressTrait == null ? null : stats.stressTrait.Id },
                { "interests", stats.skillAptitudes.Keys.Select(group => group.Id).ToList() },
                {
                    "attributes",
                    stats.StartingLevels.Where(level => level.Value != 0).ToDictionary(level => level.Key, level => level.Value)
                }
            };
        }

        private static Dictionary<string, object> Describe(CarePackageInfo package)
        {
            GameObject prefab = Assets.TryGetPrefab(package.id);
            return new Dictionary<string, object>
            {
                { "kind", "care_package" },
                { "item", package.id },
                { "name", prefab == null ? package.id : STRINGS.UI.StripLinkFormatting(prefab.GetProperName()) },
                { "quantity", ApiValues.Number(package.quantity, 1) }
            };
        }
    }
}
