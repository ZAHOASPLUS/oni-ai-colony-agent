using System;
using System.IO;
using System.Reflection;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;

namespace ActionExecutorHarness
{
    // Runs the executor's pure command checks - condition shapes and the
    // approval receipt - on commands built and approved by the Python side, so
    // a cross-language mismatch shows up offline instead of as a rejected
    // command in a live run (code review 2026-09-26, M1 / executable harness).
    internal static class Program
    {
        private const BindingFlags Static = BindingFlags.Static | BindingFlags.NonPublic;

        private static int Main(string[] args)
        {
            try
            {
                JObject input;
                // The executor reads command files with date coercion disabled;
                // approved_at must stay byte-identical for the receipt check.
                using (var reader = new JsonTextReader(new StreamReader(args[0])) { DateParseHandling = DateParseHandling.None })
                {
                    input = JObject.Load(reader);
                }
                MethodInfo shapes = typeof(ActionExecutor).GetMethod("ValidateConditionShapes", Static);
                MethodInfo receipt = typeof(ActionExecutor).GetMethod("ValidateApprovalReceipt", Static);
                if (shapes == null || receipt == null)
                {
                    throw new MissingMethodException("ActionExecutor command checks were not found");
                }
                var results = new JArray();
                foreach (JObject item in (JArray)input["cases"])
                {
                    JObject command = (JObject)item["command"];
                    JObject policy = (JObject)item["policy"];
                    string action = (string)command["action"];
                    results.Add(new JObject
                    {
                        ["name"] = item["name"],
                        ["shape_error"] = (string)shapes.Invoke(null, new object[] { command, action }),
                        ["receipt_error"] = (string)receipt.Invoke(null, new object[] { command, policy }),
                    });
                }
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.action-executor-harness.v1",
                    ["results"] = results,
                }.ToString(Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
        }
    }
}
