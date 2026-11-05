using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;

namespace AutosaveGuardHarness
{
    internal static class BuildingSettingsTests
    {
        internal static void Run(List<string> passed)
        {
            // Real game properties, without constructing or initializing Unity.
            MassageTable table = (MassageTable)RuntimeHelpers.GetUninitializedObject(typeof(MassageTable));
            table.ActivateValue = 50;
            table.DeactivateValue = 5;
            Apply(table, JObject.Parse("{\"massage_start_stress\":40,\"massage_stop_stress\":10}"));
            Require(table.ActivateValue == 40 && table.DeactivateValue == 10, "massage thresholds did not round-trip");
            Apply(table, JObject.Parse("{\"massage_stop_stress\":20}"));
            Require(table.ActivateValue == 40 && table.DeactivateValue == 20, "partial massage update changed start");
            Apply(table, JObject.Parse("{\"massage_start_stress\":100,\"massage_stop_stress\":0}"));
            Require(table.ActivateValue == 100 && table.DeactivateValue == 0, "massage endpoint range was rejected");
            Apply(table, JObject.Parse("{\"massage_start_stress\":50,\"massage_stop_stress\":50}"));
            Require(table.ActivateValue == 50 && table.DeactivateValue == 50, "equal thresholds should match UI limits");
            passed.Add("massage_range_round_trip_and_partial_update");

            foreach (string json in new[]
            {
                "{\"massage_start_stress\":49}", // would cross unchanged stop
                "{\"massage_stop_stress\":51}", // would cross unchanged start
                "{\"massage_start_stress\":40,\"massage_stop_stress\":60}",
                "{\"massage_start_stress\":-1}",
                "{\"massage_stop_stress\":101}",
                "{\"massage_start_stress\":50.5}",
                "{\"massage_start_stress\":\"50\"}",
                "{\"massage_stop_stress\":null}",
                "{\"massage_start_stress\":true}"
            })
            {
                ExpectRefusal(() => Apply(table, JObject.Parse(json)), "invalid_argument");
                Require(table.ActivateValue == 50 && table.DeactivateValue == 50, "invalid range partly changed massage settings");
            }
            passed.Add("massage_invalid_range_preserves_both_values");

            Require(ApiBuildingSettings.ReadAssignment(new JValue("")) == "", "empty string did not mean release");
            Require(ApiBuildingSettings.ReadAssignment(new JValue("101")) == "101", "numeric id string changed");
            foreach (JToken invalid in new JToken[] { null, JValue.CreateNull(), new JValue(101), new JValue(false), new JArray() })
            {
                ExpectRefusal(() => ApiBuildingSettings.ReadAssignment(invalid), "invalid_argument");
            }
            passed.Add("assignment_release_and_argument_types");

            // The shared resolver that Configure uses must not assign the first
            // same-named duplicant. Numeric ids disambiguate the same population.
            var first = new Candidate { Id = 101, Name = "Mae" };
            var second = new Candidate { Id = 102, Name = "Mae" };
            var unique = new Candidate { Id = 103, Name = "Abe" };
            var living = new List<Candidate> { first, second, unique };
            ApiException ambiguous = ExpectRefusal(() => ApiDuplicants.Pick(living, new JValue("Mae"), "assigned_duplicant", c => c.Id, c => c.Name), "ambiguous_duplicant");
            Require((int)ambiguous.Details["candidate_ids"][0] == 101 && (int)ambiguous.Details["candidate_ids"][1] == 102, "ambiguity lost candidate ids");
            Require(ReferenceEquals(ApiDuplicants.Pick(living, new JValue("102"), "assigned_duplicant", c => c.Id, c => c.Name), second), "id assigned wrong duplicant");
            Require(ReferenceEquals(ApiDuplicants.Pick(living, new JValue("Abe"), "assigned_duplicant", c => c.Id, c => c.Name), unique), "unique name failed");
            ExpectRefusal(() => ApiDuplicants.Pick(living, new JValue("999"), "assigned_duplicant", c => c.Id, c => c.Name), "unknown_duplicant");
            passed.Add("assignment_shared_name_refused_and_id_resolved");
        }

        private static void Apply(MassageTable table, JObject args)
        {
            float[] range = ApiBuildingSettings.ReadMassageRange(args, table.ActivateValue, table.DeactivateValue);
            ApiBuildingSettings.ApplyMassageRange(table, range);
        }

        private static ApiException ExpectRefusal(System.Action action, string code)
        {
            try
            {
                action();
            }
            catch (ApiException ex)
            {
                Require(ex.Code == code, "wrong refusal: " + ex.Code);
                return ex;
            }
            throw new InvalidOperationException("expected refusal " + code);
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }

        private sealed class Candidate
        {
            internal int Id;
            internal string Name;
        }
    }
}
