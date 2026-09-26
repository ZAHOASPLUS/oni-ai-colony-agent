using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Security;

namespace OniAiProbeMod.Security
{
    internal static class Program
    {
        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            try
            {
                string vectorPath = args.Length > 0
                    ? Path.GetFullPath(args[0])
                    : Path.Combine(AppContext.BaseDirectory, "vectors.json");
                JObject vectors = CanonicalHashContract.ParseObjectStrict(
                    File.ReadAllText(vectorPath, Encoding.UTF8));
                Require(
                    string.Equals(
                        (string)vectors["contract"],
                        CanonicalHashContract.ContractId,
                        StringComparison.Ordinal),
                    "vector contract does not match the C# implementation");

                JObject caseHashes = new JObject();
                int caseCount = VerifyCases((JArray)vectors["cases"], caseHashes);
                int equivalentCount = VerifyEquivalentPairs((JArray)vectors["equivalent_pairs"]);
                int tamperCount = VerifyTamperPairs((JArray)vectors["tamper_pairs"]);
                int invalidCount = VerifyInvalidJson((JArray)vectors["invalid_json"]);
                invalidCount += VerifyProgrammaticRejections();

                JObject result = new JObject
                {
                    ["status"] = "passed",
                    ["contract"] = CanonicalHashContract.ContractId,
                    ["case_count"] = caseCount,
                    ["equivalent_pair_count"] = equivalentCount,
                    ["tamper_pair_count"] = tamperCount,
                    ["invalid_count"] = invalidCount,
                    ["case_hashes"] = caseHashes
                };
                Console.WriteLine(result.ToString(Formatting.None));
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(
                    "FAIL canonical hash harness: " + error.GetType().Name + ": " + error.Message);
                return 1;
            }
        }

        private static int VerifyCases(JArray cases, JObject output)
        {
            Require(cases != null && cases.Count > 0, "canonical cases are missing");
            HashSet<string> names = new HashSet<string>(StringComparer.Ordinal);
            foreach (JObject item in cases.Children<JObject>())
            {
                string name = (string)item["name"];
                string expected = (string)item["expected_hash"];
                Require(!string.IsNullOrEmpty(name) && names.Add(name), "case name is missing or duplicate");
                string actual = CanonicalHashContract.Hash(item["value"]);
                Require(
                    string.Equals(actual, expected, StringComparison.Ordinal),
                    name + " expected " + expected + " but C# produced " + actual);
                output[name] = actual;
            }
            return names.Count;
        }

        private static int VerifyEquivalentPairs(JArray pairs)
        {
            Require(pairs != null, "equivalent pairs are missing");
            foreach (JObject item in pairs.Children<JObject>())
            {
                string left = CanonicalHashContract.Hash(item["left"]);
                string right = CanonicalHashContract.Hash(item["right"]);
                Require(
                    string.Equals(left, right, StringComparison.Ordinal),
                    (string)item["name"] + " changed under object insertion order");
            }
            return pairs.Count;
        }

        private static int VerifyTamperPairs(JArray pairs)
        {
            Require(pairs != null, "tamper pairs are missing");
            foreach (JObject item in pairs.Children<JObject>())
            {
                string left = CanonicalHashContract.Hash(item["left"]);
                string right = CanonicalHashContract.Hash(item["right"]);
                Require(
                    !string.Equals(left, right, StringComparison.Ordinal),
                    (string)item["name"] + " did not change the canonical hash");
            }
            return pairs.Count;
        }

        private static int VerifyInvalidJson(JArray invalidCases)
        {
            Require(invalidCases != null, "invalid JSON cases are missing");
            foreach (JObject item in invalidCases.Children<JObject>())
            {
                bool rejected = false;
                try
                {
                    JObject ignored = CanonicalHashContract.ParseObjectStrict((string)item["json"]);
                    CanonicalHashContract.Hash(ignored);
                }
                catch (Exception)
                {
                    rejected = true;
                }
                Require(rejected, (string)item["name"] + " was not rejected");
            }
            return invalidCases.Count;
        }

        private static int VerifyProgrammaticRejections()
        {
            RejectToken("NaN", new JValue(double.NaN));
            RejectToken("positive infinity", new JValue(double.PositiveInfinity));
            RejectToken("negative infinity", new JValue(double.NegativeInfinity));
            RejectToken("unpaired surrogate", new JValue("\ud800"));
            RejectToken("non-tree DateTime", new JValue(DateTime.UtcNow));
            return 5;
        }

        private static void RejectToken(string name, JToken value)
        {
            bool rejected = false;
            try
            {
                CanonicalHashContract.Hash(value);
            }
            catch (CanonicalHashException)
            {
                rejected = true;
            }
            Require(rejected, name + " token was not rejected");
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
