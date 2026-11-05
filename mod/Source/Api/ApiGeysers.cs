using System.Collections.Generic;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Geyser study, as the Studyable side screen does it, and the numbers the
    /// game shows once a geyser is studied.
    /// </summary>
    internal static class ApiGeysers
    {
        private const float SecondsPerCycle = 600f;

        // Studyable.markedForStudy is private; the side screen reads it the same way.
        private static readonly AccessTools.FieldRef<Studyable, bool> Marked =
            AccessTools.FieldRefAccess<Studyable, bool>("markedForStudy");

        internal static void Describe(Geyser geyser, Dictionary<string, object> entry)
        {
            entry["id"] = geyser.GetComponent<KPrefabID>().InstanceID;
            Studyable study = geyser.GetComponent<Studyable>();
            bool studied = study != null && study.Studied;
            entry["studied"] = studied;
            if (study != null && !studied)
            {
                entry["study_requested"] = Marked(study);
                entry["studying"] = study.Studying;
            }
            if (!studied)
            {
                return;
            }
            GeyserConfigurator.GeyserInstanceConfiguration config = geyser.configuration;
            entry["kg_per_cycle_active"] = ApiValues.Number(config.GetMassPerCycle(), 1);
            // GetAverageEmission is kg/s over eruptions, pauses and dormancy.
            entry["average_kg_per_cycle"] = ApiValues.Number(config.GetAverageEmission() * SecondsPerCycle, 1);
            entry["eruption_cycles"] = ApiValues.Number(config.GetOnDuration() / SecondsPerCycle, 2);
            entry["pause_cycles"] = ApiValues.Number(config.GetOffDuration() / SecondsPerCycle, 2);
            entry["active_cycles"] = ApiValues.Number(config.GetYearOnDuration() / SecondsPerCycle, 1);
            entry["dormant_cycles"] = ApiValues.Number(config.GetYearOffDuration() / SecondsPerCycle, 1);
            bool dormant = geyser.ShouldGoDormant();
            entry["dormant_now"] = dormant;
            entry["phase_cycles_left"] = ApiValues.Number(
                (dormant ? geyser.RemainingDormantTime() : geyser.RemainingActiveTime()) / SecondsPerCycle, 1);
        }

        /// <summary>
        /// study: true sends a duplicant to analyse the geyser, false cancels the
        /// request.  Setting what is already set changes nothing.  The work is
        /// the game's own chore, with its skill and priority rules.
        /// </summary>
        internal static bool Configure(GameObject target, JObject args, Dictionary<string, object> applied)
        {
            JToken value = args["study"];
            if (value == null)
            {
                return false;
            }
            if (value.Type != JTokenType.Boolean)
            {
                throw new ApiException("invalid_argument", "study must be true or false.");
            }
            Studyable study = target.GetComponent<Studyable>();
            if (study == null)
            {
                throw new ApiException("unsupported_setting", "Only something the game lets you analyse, such as a geyser, takes study.");
            }
            bool wanted = value.Value<bool>();
            if (study.Studied)
            {
                applied["study"] = "already_studied";
                return true;
            }
            if (Marked(study) != wanted)
            {
                // StudyableSideScreen's button calls exactly this; it toggles.
                study.OnSidescreenButtonPressed();
            }
            applied["study"] = Marked(study) ? "requested" : "not_requested";
            return true;
        }
    }
}
