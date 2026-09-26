using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;

namespace FunctionalBuildingsHarness
{
    // Checks the functional-building reader's prefab coverage: every supported
    // id is a real building id in the installed game (except the legacy
    // OxygenDiffuser alias the Python contract keeps), the list matches the
    // checked-in contract, and FlushToilet is covered for sanitation.
    internal static class Program
    {
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;

        // Not a buildable prefab in the current game; kept as an explicit alias.
        private static readonly string[] LegacyIds = { "OxygenDiffuser" };

        private static int Main(string[] args)
        {
            try
            {
                List<string> passed = Run(args[0]);
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.functional-buildings-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(passed.ToArray())
                }.ToString(Newtonsoft.Json.Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
        }

        private static List<string> Run(string contractPath)
        {
            List<string> passed = new List<string>();
            Type reader = typeof(FunctionalBuildingTelemetryReader);
            string[] supported = (string[])reader.GetField("SupportedPrefabIds", AnyStatic).GetValue(null);
            MethodInfo isSupported = reader.GetMethod("IsSupportedPrefab", AnyStatic);
            Require(supported.Length == supported.Distinct(StringComparer.Ordinal).Count(), "supported ids repeat");

            Assembly game = typeof(BuildingDef).Assembly;
            foreach (string id in supported)
            {
                Require((bool)isSupported.Invoke(null, new object[] { id }), id + " is not supported by the reader");
                if (LegacyIds.Contains(id))
                {
                    continue;
                }
                Type config = game.GetType(id + "Config");
                FieldInfo idField = config?.GetField("ID", BindingFlags.Public | BindingFlags.Static);
                Require(idField != null && (string)idField.GetRawConstantValue() == id,
                    id + " is not a building id in the installed game");
            }
            Require(supported.Contains("FlushToilet"), "FlushToilet is not covered");
            Passed(passed, "supported_ids_are_game_buildings");

            JObject contract = JObject.Parse(File.ReadAllText(contractPath));
            string[] declared = contract["supported_prefab_ids"].Select(item => (string)item).ToArray();
            Require(declared.SequenceEqual(supported.OrderBy(id => id, StringComparer.Ordinal)),
                "the contract's supported_prefab_ids differ from the reader: " + string.Join(",", declared));
            Passed(passed, "contract_matches_reader");

            // Every payload (observed or error) declares the sorted coverage list.
            MethodInfo payload = reader.GetMethod("Payload", AnyStatic);
            Type instanceType = reader.GetNestedType("FunctionalBuildingInstance", BindingFlags.NonPublic);
            object noInstances = Activator.CreateInstance(typeof(List<>).MakeGenericType(instanceType));
            string[] allowedKeys = contract["payload_keys"].Concat(contract["optional_payload_keys"])
                .Select(item => (string)item)
                .OrderBy(key => key, StringComparer.Ordinal)
                .ToArray();
            foreach (bool complete in new[] { true, false })
            {
                var emitted = (Dictionary<string, object>)payload.Invoke(null, new object[]
                {
                    DateTimeOffset.UtcNow, 0, complete, noInstances,
                    complete ? "observed" : "error", complete ? null : "harness"
                });
                string[] publicKeys = emitted.Keys
                    .Where(key => !key.StartsWith("_", StringComparison.Ordinal))
                    .OrderBy(key => key, StringComparer.Ordinal)
                    .ToArray();
                Require(publicKeys.SequenceEqual(allowedKeys), "payload keys differ from the contract: " + string.Join(",", publicKeys));
                string[] prefabIds = ((List<object>)emitted["prefab_ids"]).Cast<string>().ToArray();
                Require(prefabIds.Length > 0 && prefabIds.SequenceEqual(declared), "prefab_ids is not the sorted supported list");
            }
            Passed(passed, "payload_declares_sorted_prefab_ids");
            return passed;
        }

        private static void Passed(List<string> passed, string name)
        {
            passed.Add(name);
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}
