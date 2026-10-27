using System;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;
using OniAiProbeMod.Probe;

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
                    // A check that needs live game state (Grid, Db) throws here;
                    // report it per case instead of failing the whole run.
                    var result = new JObject { ["name"] = item["name"] };
                    string shapeError = Invoke(shapes, new object[] { command, action }, result, "shape_exception");
                    result["shape_error"] = shapeError;
                    result["full_shape_validated"] = shapeError == null && result.Property("shape_exception") == null;
                    result["receipt_error"] = Invoke(receipt, new object[] { command, policy }, result, "receipt_exception");
                    // This is a separate, pure contract check, never a substitute
                    // for the full shape/native BuildingDef validation above.
                    bool utility = action == "place_utility_blueprint_path" || action == "cancel_utility_blueprint_path";
                    if (action == "place_building_blueprint" || action == "place_utility_blueprint_path")
                    {
                        MethodInfo cellContract = typeof(ActionExecutor).GetMethod("ValidateBlueprintPlaceCellContract", Static);
                        if (cellContract == null) throw new MissingMethodException("Blueprint place cell contract not found");
                        string defId = (string)command["args"]["building_def_id"];
                        var cellResults = new JArray();
                        foreach (JToken cell in (JArray)command["args"]["cells"])
                        {
                            var cellResult = new JObject { ["cell"] = cell };
                            cellResult["error"] = Invoke(cellContract, new object[] {
                                (int)cell, defId, utility, ProbeStateReader.BlueprintCanPlaceField(defId),
                                (JArray)command["args"]["material_tags"],
                                (JArray)command["preconditions"], (JArray)command["expected_effects"]
                            }, cellResult, "exception");
                            cellResults.Add(cellResult);
                        }
                        result["cell_contracts"] = cellResults;
                    }
                    else if (action == "cancel_building_blueprint" || action == "cancel_utility_blueprint_path")
                    {
                        MethodInfo cellContract = typeof(ActionExecutor).GetMethod("ValidateBlueprintCancelCellContract", Static);
                        Type targetType = typeof(ActionExecutor).GetNestedType("BlueprintCancelTarget", BindingFlags.NonPublic);
                        if (cellContract == null || targetType == null) throw new MissingMethodException("Blueprint cancel contract not found");
                        var cellResults = new JArray();
                        foreach (JObject target in (JArray)command["args"]["targets"])
                        {
                            int cell = (int)target["cell"];
                            string prefabPath = "cells.by_id.cell_" + cell + ".blueprint_prefab_id";
                            string defId = (string)((JArray)command["preconditions"])
                                .OfType<JObject>().Single(condition => (string)condition["path"] == prefabPath)["value"];
                            object typedTarget = Activator.CreateInstance(targetType, true);
                            targetType.GetField("Cell", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(typedTarget, cell);
                            targetType.GetField("BlueprintInstanceId", BindingFlags.Instance | BindingFlags.NonPublic)
                                .SetValue(typedTarget, (int)target["blueprint_instance_id"]);
                            var cellResult = new JObject { ["cell"] = cell };
                            cellResult["error"] = Invoke(cellContract, new object[] {
                                typedTarget, defId, utility, ProbeStateReader.BlueprintCanPlaceField(defId),
                                (JArray)command["preconditions"], (JArray)command["expected_effects"]
                            }, cellResult, "exception");
                            cellResults.Add(cellResult);
                        }
                        result["cell_contracts"] = cellResults;
                    }
                    results.Add(result);
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

        private static string Invoke(MethodInfo method, object[] arguments, JObject result, string exceptionField)
        {
            try
            {
                return (string)method.Invoke(null, arguments);
            }
            catch (TargetInvocationException ex) when (ex.InnerException != null)
            {
                result[exceptionField] = ex.InnerException.GetType().Name + ": " + ex.InnerException.Message;
                return null;
            }
        }
    }
}
