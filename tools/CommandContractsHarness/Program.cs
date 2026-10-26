using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Contracts;
using OniAiProbeMod.Probe;

namespace OniAiProbeMod.Contracts
{
    internal static class Program
    {
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            try
            {
                JObject paused = State(
                    "game.paused",
                    false,
                    Entry(
                        CommandFieldContracts.GamePausedId,
                        "SpeedControlScreen.IsPaused",
                        "direct_state",
                        "boolean",
                        "active_game_session",
                        "observed",
                        "complete"));
                JObject pausedCondition = Condition(
                    "game.paused",
                    false,
                    CommandFieldContracts.GamePausedId,
                    "observed");
                Accept("valid_game_paused", paused, pausedCondition);
                RejectMetadataTampering(paused, pausedCondition);

                JObject booleanNumericMismatch = (JObject)pausedCondition.DeepClone();
                booleanNumericMismatch["op"] = "ne";
                booleanNumericMismatch["value"] = 0;
                Reject("reject_boolean_numeric_operand", paused, booleanNumericMismatch);

                JObject scalarQuality = (JObject)pausedCondition.DeepClone();
                scalarQuality["quality"] = "observed";
                Reject("reject_scalar_quality_allowance", paused, scalarQuality);

                JObject duplicateQuality = (JObject)pausedCondition.DeepClone();
                duplicateQuality["quality"] = new JArray("observed", "observed");
                Reject("reject_duplicate_quality_allowance", paused, duplicateQuality);

                JObject research = State(
                    "research.current",
                    "SpaceProgram",
                    Entry(
                        CommandFieldContracts.ResearchCurrentId,
                        "Research.GetActiveResearch",
                        "active_selection",
                        "technology_id_or_empty",
                        "save_research",
                        "observed",
                        "complete"));
                JObject researchContains = Condition(
                    "research.current",
                    "",
                    CommandFieldContracts.ResearchCurrentId,
                    "observed");
                researchContains["op"] = "contains";
                Reject("reject_string_contains_operand", research, researchContains);

                JObject buildingCount = State(
                    "buildings.oxygen_diffuser",
                    0,
                    Entry(
                        CommandFieldContracts.BuildingCompletedCountId,
                        CommandFieldContracts.BuildingCompletedCountSource,
                        "completed_prefab_instance_count",
                        "count",
                        "active_world",
                        "observed",
                        "complete"));
                // The completed-prefab count is diagnostic: the Bridge consumes it,
                // no command may bind it, and TryResolve refuses the path.
                Reject(
                    "reject_building_count_precondition",
                    buildingCount,
                    Condition(
                        "buildings.oxygen_diffuser",
                        0,
                        CommandFieldContracts.BuildingCompletedCountId,
                        "observed"));

                JObject researchComplete = State(
                    "research.complete",
                    new JArray("FarmingTech", "ImprovedOxygen"),
                    Entry(
                        CommandFieldContracts.ResearchCompleteId,
                        CommandFieldContracts.ResearchCompleteSource,
                        "completed_technology_ids",
                        "technology_id_list",
                        "save_research",
                        "observed",
                        "complete"));
                JObject researchCompleteContains = Condition(
                    "research.complete",
                    "ImprovedOxygen",
                    CommandFieldContracts.ResearchCompleteId,
                    "observed");
                researchCompleteContains["op"] = "contains";
                Accept("valid_research_complete_contains", researchComplete, researchCompleteContains);
                RejectMutationEligibility(
                    "reject_research_complete_mutation_eligibility",
                    researchCompleteContains);
                JObject researchCompleteEquality = (JObject)researchCompleteContains.DeepClone();
                researchCompleteEquality["op"] = "eq";
                researchCompleteEquality["value"] = new JArray("FarmingTech", "ImprovedOxygen");
                Accept("valid_research_complete_equality", researchComplete, researchCompleteEquality);
                JObject researchCompleteWrongSource = (JObject)researchComplete.DeepClone();
                researchCompleteWrongSource["quality"]["research.complete"]["source"] = "Research.GetActiveResearch";
                Reject("reject_research_complete_wrong_source", researchCompleteWrongSource, researchCompleteContains);
                JObject unsortedComplete = (JObject)researchComplete.DeepClone();
                unsortedComplete["research"]["complete"] = new JArray("ImprovedOxygen", "FarmingTech");
                Reject("reject_unsorted_research_complete", unsortedComplete, researchCompleteContains);
                JObject duplicateComplete = (JObject)researchComplete.DeepClone();
                duplicateComplete["research"]["complete"] = new JArray("FarmingTech", "FarmingTech");
                Reject("reject_duplicate_research_complete", duplicateComplete, researchCompleteContains);
                JObject malformedComplete = (JObject)researchComplete.DeepClone();
                malformedComplete["research"]["complete"] = new JArray("Improved Oxygen");
                Reject("reject_malformed_research_complete_id", malformedComplete, researchCompleteContains);
                JObject scalarComplete = (JObject)researchComplete.DeepClone();
                scalarComplete["research"]["complete"] = "ImprovedOxygen";
                Reject("reject_scalar_research_complete", scalarComplete, researchCompleteContains);
                JObject completeMalformedOperand = (JObject)researchCompleteContains.DeepClone();
                completeMalformedOperand["value"] = "Improved Oxygen";
                Reject(
                    "reject_research_complete_contains_malformed_operand",
                    researchComplete,
                    completeMalformedOperand);
                JObject completeOrderedOperator = (JObject)researchCompleteContains.DeepClone();
                completeOrderedOperator["op"] = "gt";
                Reject("reject_research_complete_ordered_operator", researchComplete, completeOrderedOperator);

                JObject foodObserved = State(
                    "resources.food_kcal",
                    12000.0,
                    Entry(
                        CommandFieldContracts.RegisteredFoodId,
                        "ClusterManager.activeWorld.worldInventory.GetPickupables(GameTags.Edible)+!GameTags.StoredPrivate",
                        "registered_non_private_inventory_sum",
                        "kcal",
                        "active_world_registered_non_private_edibles",
                        "observed",
                        "complete"));
                Accept(
                    "valid_complete_registered_food",
                    foodObserved,
                    Condition(
                        "resources.food_kcal",
                        12000.0,
                        CommandFieldContracts.RegisteredFoodId,
                        "observed"));
                RejectMutationEligibility(
                    "reject_registered_food_mutation_eligibility",
                    Condition(
                        "resources.food_kcal",
                        12000.0,
                        CommandFieldContracts.RegisteredFoodId,
                        "observed"));
                JObject excessiveFood = (JObject)foodObserved.DeepClone();
                excessiveFood["resources"]["food_kcal"] = 1_000_000_000_001d;
                Reject(
                    "reject_registered_food_above_maximum",
                    excessiveFood,
                    Condition(
                        "resources.food_kcal",
                        1_000_000_000_001d,
                        CommandFieldContracts.RegisteredFoodId,
                        "observed"));

                JObject foodBounded = (JObject)foodObserved.DeepClone();
                JObject foodBoundedQuality = (JObject)foodBounded["quality"]["resources.food_kcal"];
                foodBoundedQuality["status"] = "estimated";
                foodBoundedQuality["completeness"] = "bounded";
                Accept(
                    "valid_bounded_registered_food",
                    foodBounded,
                    Condition(
                        "resources.food_kcal",
                        12000.0,
                        CommandFieldContracts.RegisteredFoodId,
                        "estimated"));
                JObject inconsistentFood = (JObject)foodBounded.DeepClone();
                inconsistentFood["quality"]["resources.food_kcal"]["status"] = "observed";
                Reject(
                    "reject_status_completeness_pair",
                    inconsistentFood,
                    Condition(
                        "resources.food_kcal",
                        12000.0,
                        CommandFieldContracts.RegisteredFoodId,
                        "observed"));

                JObject total = State(
                    "duplicants.total",
                    3,
                    Entry(
                        CommandFieldContracts.ActiveWorldDuplicantCountId,
                        CommandFieldContracts.ActiveWorldDuplicantCountSource,
                        "live_count",
                        "count",
                        "active_world",
                        "observed",
                        "complete"));
                JObject totalPositive = Condition(
                    "duplicants.total",
                    2,
                    CommandFieldContracts.ActiveWorldDuplicantCountId,
                    "observed");
                totalPositive["op"] = "gte";
                Accept("valid_active_world_duplicant_total", total, totalPositive);
                RejectMetadataTampering(total, totalPositive);
                JObject totalWrongSource = (JObject)total.DeepClone();
                totalWrongSource["quality"]["duplicants.total"]["source"] =
                    "Components.LiveMinionIdentities.Items";
                Reject("reject_total_wrong_source", totalWrongSource, totalPositive);
                JObject totalWrongContract = (JObject)totalPositive.DeepClone();
                totalWrongContract["quality_contract_id"] = CommandFieldContracts.DuplicantIdleId;
                Reject("reject_total_wrong_contract", total, totalWrongContract);
                JObject totalFractional = (JObject)total.DeepClone();
                totalFractional["duplicants"]["total"] = 2.5d;
                Reject("reject_total_fractional_count", totalFractional, totalPositive);
                JObject totalAboveLimit = (JObject)total.DeepClone();
                totalAboveLimit["duplicants"]["total"] = 1001;
                Reject("reject_total_above_limit", totalAboveLimit, totalPositive);

                JObject idle = State(
                    "duplicants.idle",
                    1,
                    Entry(
                        CommandFieldContracts.DuplicantIdleId,
                        CommandFieldContracts.DuplicantIdleSource,
                        "idle_count",
                        "count",
                        "active_world",
                        "observed",
                        "complete"));
                JObject idlePositive = Condition(
                    "duplicants.idle",
                    0,
                    CommandFieldContracts.DuplicantIdleId,
                    "observed");
                idlePositive["op"] = "gt";
                Accept("valid_focus_note_idle_condition", idle, idlePositive);
                RejectMutationEligibility("reject_idle_mutation_eligibility", idlePositive);
                JObject idleWrongSource = (JObject)idle.DeepClone();
                idleWrongSource["quality"]["duplicants.idle"]["source"] =
                    "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+ChoreConsumer";
                Reject("reject_idle_wrong_source", idleWrongSource, idlePositive);
                JObject idleFractional = (JObject)idle.DeepClone();
                idleFractional["duplicants"]["idle"] = 0.5d;
                Reject("reject_idle_fractional_count", idleFractional, idlePositive);
                JObject idleAboveLimit = (JObject)idle.DeepClone();
                idleAboveLimit["duplicants"]["idle"] = 1001;
                Reject("reject_idle_above_limit", idleAboveLimit, idlePositive);

                JObject stressed = State(
                    "duplicants.stressed",
                    2,
                    Entry(
                        CommandFieldContracts.DuplicantStressedId,
                        CommandFieldContracts.DuplicantStressedSource,
                        "stressed_count",
                        "count",
                        "active_world",
                        "observed",
                        "complete"));
                JObject stressedPositive = Condition(
                    "duplicants.stressed",
                    1,
                    CommandFieldContracts.DuplicantStressedId,
                    "observed");
                stressedPositive["op"] = "gte";
                Accept("valid_focus_note_stressed_condition", stressed, stressedPositive);
                RejectMutationEligibility("reject_stressed_mutation_eligibility", stressedPositive);
                JObject stressedWrongContract = (JObject)stressedPositive.DeepClone();
                stressedWrongContract["quality_contract_id"] = CommandFieldContracts.DuplicantIdleId;
                Reject("reject_stressed_wrong_contract", stressed, stressedWrongContract);
                JObject stressedNegative = (JObject)stressed.DeepClone();
                stressedNegative["duplicants"]["stressed"] = -1;
                Reject("reject_stressed_negative_count", stressedNegative, stressedPositive);

                JObject alerts = State(
                    "alerts",
                    new JArray("high_stress", "low_oxygen"),
                    Entry(
                        CommandFieldContracts.CanonicalAlertsId,
                        CanonicalAlertReader.Source,
                        "canonical_active_alert_ids",
                        "alert_id_list",
                        CanonicalAlertReader.Scope,
                        "observed",
                        "complete"));
                JObject alertsContains = Condition(
                    "alerts",
                    "low_oxygen",
                    CommandFieldContracts.CanonicalAlertsId,
                    "observed");
                alertsContains["op"] = "contains";
                alerts["quality"]["alerts"]["reason"] = "diagnostic_only";
                Accept("valid_canonical_alerts", alerts, alertsContains);
                JObject unknownAlertCondition = (JObject)alertsContains.DeepClone();
                unknownAlertCondition["value"] = "unknown_hazard";
                Reject(
                    "reject_unknown_alert_condition_operand",
                    alerts,
                    unknownAlertCondition);
                JObject legacyAlertSource = (JObject)alerts.DeepClone();
                legacyAlertSource["quality"]["alerts"]["source"] =
                    "AlertManager.ActiveWorldCanonical";
                Reject(
                    "reject_legacy_alert_source",
                    legacyAlertSource,
                    alertsContains);
                JObject nonCanonicalAlerts = (JObject)alerts.DeepClone();
                nonCanonicalAlerts["alerts"] = new JArray("Low Oxygen!");
                Reject(
                    "reject_noncanonical_alert_id",
                    nonCanonicalAlerts,
                    alertsContains);
                JObject duplicateAlerts = (JObject)alerts.DeepClone();
                duplicateAlerts["alerts"] = new JArray("low_oxygen", "low_oxygen");
                Reject(
                    "reject_duplicate_alert_id",
                    duplicateAlerts,
                    alertsContains);
                JObject unsortedAlerts = (JObject)alerts.DeepClone();
                unsortedAlerts["alerts"] = new JArray("low_oxygen", "high_stress");
                Reject(
                    "reject_unsorted_canonical_alert_ids",
                    unsortedAlerts,
                    alertsContains);
                JObject unknownAlerts = (JObject)alerts.DeepClone();
                unknownAlerts["alerts"] = new JArray("unknown_hazard");
                Reject(
                    "reject_unknown_canonical_alert_id",
                    unknownAlerts,
                    alertsContains);

                const string priorityPath =
                    "duplicants.by_instance_id.id_123.chore_priorities.Dig";
                JObject priority = State(
                    priorityPath,
                    2,
                    Entry(
                        CommandFieldContracts.DuplicantPriorityId,
                        "Components.LiveMinionIdentities+ChoreConsumer",
                        "configured_chore_priority",
                        "priority_0_to_5",
                        "active_world_live_duplicant",
                        "observed",
                        "complete"));
                Accept(
                    "valid_dynamic_priority",
                    priority,
                    Condition(priorityPath, 2, CommandFieldContracts.DuplicantPriorityId, "observed"));
                JObject invalidPriority = (JObject)priority.DeepClone();
                SetPath(invalidPriority, priorityPath, 6);
                Reject(
                    "reject_priority_range",
                    invalidPriority,
                    Condition(priorityPath, 6, CommandFieldContracts.DuplicantPriorityId, "observed"));

                AcceptDynamic(
                    "valid_dig_tool_contract",
                    "designations.dig.tool.tiles_filter_on",
                    true,
                    CommandFieldContracts.DigToolId,
                    "Grid+DigTool+Components.Diggables",
                    "dig_tool_filter_state",
                    "boolean",
                    "active_game_session",
                    "complete");
                AcceptDynamic(
                    "valid_dig_cell_contract",
                    "designations.dig.by_cell.cell_456.safe_mark_candidate",
                    true,
                    CommandFieldContracts.DigCellId,
                    "Grid+DigTool+Components.Diggables",
                    "dig_cell_state_field",
                    "field_native",
                    "active_world_tracked_cells",
                    "complete_for_tracked_cell");
                AcceptDynamic(
                    "valid_blueprint_catalog_contract",
                    "blueprints.catalog.Ladder.available",
                    true,
                    CommandFieldContracts.BlueprintCatalogId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "blueprint_catalog_field",
                    "field_native",
                    "approved_blueprint_catalog",
                    "complete");
                AcceptDynamic(
                    "valid_blueprint_cell_contract",
                    "cells.by_id.cell_500.fingerprint",
                    "sha256:" + new string('a', 64),
                    CommandFieldContracts.BlueprintCellId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "blueprint_cell_state_field",
                    "field_native",
                    "active_world_tracked_cells",
                    "complete_for_tracked_cell");
                AcceptDynamic(
                    "valid_oxygen_building_catalog_contract",
                    "blueprints.catalog.MineralDeoxidizer.available",
                    true,
                    CommandFieldContracts.BlueprintCatalogId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "blueprint_catalog_field",
                    "field_native",
                    "approved_blueprint_catalog",
                    "complete");
                AcceptDynamic(
                    "valid_oxygen_building_cell_contract",
                    "cells.by_id.cell_500.can_place_algae_habitat",
                    true,
                    CommandFieldContracts.BlueprintCellId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "blueprint_cell_state_field",
                    "field_native",
                    "active_world_tracked_cells",
                    "complete_for_tracked_cell");
                // Electrolyzer was the example until it was allowlisted (2026-10-26
                // round 44); LiquidPump is still outside the allowlist.
                JObject unlistedCatalog = State(
                    "blueprints.catalog.LiquidPump.available",
                    true,
                    Entry(
                        CommandFieldContracts.BlueprintCatalogId,
                        "BuildingDef+Grid+Constructable+WorldInventory",
                        "blueprint_catalog_field",
                        "field_native",
                        "approved_blueprint_catalog",
                        "observed",
                        "complete"));
                Reject(
                    "reject_unlisted_building_catalog_path",
                    unlistedCatalog,
                    Condition(
                        "blueprints.catalog.LiquidPump.available",
                        true,
                        CommandFieldContracts.BlueprintCatalogId,
                        "observed"));
                AcceptDynamic(
                    "valid_blueprint_instance_contract",
                    "blueprints.by_instance_id.id_9001.instance_id",
                    9001,
                    CommandFieldContracts.BlueprintInstanceId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "blueprint_instance_state_field",
                    "field_native",
                    "active_world_registered_blueprints",
                    "complete_for_registered_instance");
                AcceptDynamic(
                    "valid_utility_path_contract",
                    "blueprints.utility_paths.id_wirepath1.active",
                    true,
                    CommandFieldContracts.UtilityPathId,
                    "BuildingDef+Grid+Constructable+WorldInventory",
                    "utility_blueprint_path_state_field",
                    "field_native",
                    "probe_tracked_utility_paths",
                    "complete_for_tracked_path");

                JObject stale = (JObject)paused.DeepClone();
                stale["ts"] = DateTimeOffset.Now.AddSeconds(-10).ToString("o");
                Reject("reject_stale_capture", stale, pausedCondition);

                JObject missingContract = (JObject)pausedCondition.DeepClone();
                missingContract.Remove("quality_contract_id");
                Reject("reject_missing_contract_id", paused, missingContract);

                JObject unknownPath = Condition(
                    "materials.active_world_cells.clean_oxygen_kg",
                    0.0,
                    "oni.qc.fake.v1",
                    "observed");
                Reject("reject_unknown_path_contract", paused, unknownPath);

                Console.WriteLine(
                    new JObject
                    {
                        ["schema"] = "oni.command-contract-harness.v1",
                        ["status"] = "passed",
                        ["passed_count"] = Passed.Count,
                        ["cases"] = new JArray(Passed)
                    }.ToString());
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
        }

        private static JObject State(string path, object value, JObject qualityEntry)
        {
            JObject state = new JObject
            {
                ["ts"] = DateTimeOffset.Now.ToString("o"),
                ["quality"] = new JObject { [path] = qualityEntry }
            };
            SetPath(state, path, ToToken(value));
            return state;
        }

        private static void SetPath(JObject state, string path, object value)
        {
            string[] parts = path.Split('.');
            JObject current = state;
            for (int index = 0; index < parts.Length - 1; index++)
            {
                if (!(current[parts[index]] is JObject next))
                {
                    next = new JObject();
                    current[parts[index]] = next;
                }
                current = next;
            }
            current[parts[parts.Length - 1]] = ToToken(value);
        }

        private static JObject Entry(
            string contractId,
            string source,
            string semantic,
            string unit,
            string scope,
            string status,
            string completeness)
        {
            return new JObject
            {
                ["status"] = status,
                ["source"] = source,
                ["contract_id"] = contractId,
                ["semantic"] = semantic,
                ["unit"] = unit,
                ["scope"] = scope,
                ["completeness"] = completeness,
                ["captured_at"] = "snapshot.ts"
            };
        }

        private static JObject Condition(
            string path,
            object value,
            string contractId,
            string status)
        {
            return new JObject
            {
                ["path"] = path,
                ["op"] = "eq",
                ["value"] = ToToken(value),
                ["quality"] = new JArray(status),
                ["quality_contract_id"] = contractId
            };
        }

        private static void RejectMetadataTampering(JObject state, JObject condition)
        {
            string path = condition["path"]?.ToString();
            if (string.IsNullOrEmpty(path))
            {
                throw new InvalidOperationException("Metadata tamper fixture has no condition path.");
            }
            foreach (string field in new[]
            {
                "source",
                "contract_id",
                "semantic",
                "unit",
                "scope",
                "completeness",
                "captured_at"
            })
            {
                JObject candidate = (JObject)state.DeepClone();
                JObject quality = candidate["quality"]?[path] as JObject;
                if (quality == null)
                {
                    throw new InvalidOperationException("Metadata tamper fixture has no quality for " + path + ".");
                }
                quality[field] = "tampered";
                Reject("reject_" + path.Replace('.', '_') + "_tampered_" + field, candidate, condition);
            }
        }

        private static void AcceptDynamic(
            string name,
            string path,
            object value,
            string contractId,
            string source,
            string semantic,
            string unit,
            string scope,
            string completeness)
        {
            JObject state = State(
                path,
                value,
                Entry(
                    contractId,
                    source,
                    semantic,
                    unit,
                    scope,
                    "observed",
                    completeness));
            Accept(name, state, Condition(path, value, contractId, "observed"));
        }

        private static void RejectMutationEligibility(string name, JObject condition)
        {
            if (CommandFieldContracts.MutationEligible(condition, out _))
            {
                throw new InvalidOperationException(name + " unexpectedly allowed mutation.");
            }
            Passed.Add(name);
        }

        private static JToken ToToken(object value)
        {
            if (value == null)
            {
                return JValue.CreateNull();
            }
            if (value is JToken token)
            {
                return token;
            }
            if (value is string text)
            {
                return new JValue(text);
            }
            if (value is bool boolean)
            {
                return new JValue(boolean);
            }
            if (value is int integer)
            {
                return new JValue(integer);
            }
            if (value is long longInteger)
            {
                return new JValue(longInteger);
            }
            if (value is float single)
            {
                return new JValue(single);
            }
            if (value is double number)
            {
                return new JValue(number);
            }
            throw new ArgumentException("Unsupported harness token type: " + value.GetType().FullName);
        }

        private static void Accept(string name, JObject state, JObject condition)
        {
            bool declarationAccepted = CommandFieldContracts.DeclarationMatches(
                condition,
                out string declarationError);
            bool stateAccepted = false;
            string stateError = null;
            if (declarationAccepted)
            {
                stateAccepted = CommandFieldContracts.ValidateCurrentState(
                    state,
                    condition,
                    state.SelectToken(condition["path"].ToString(), false),
                    out stateError);
            }
            if (!declarationAccepted || !stateAccepted)
            {
                throw new InvalidOperationException(
                    name + " unexpectedly failed: " + declarationError + " " + stateError);
            }
            Passed.Add(name);
        }

        private static void Reject(string name, JObject state, JObject condition)
        {
            bool declared = CommandFieldContracts.DeclarationMatches(condition, out _);
            JToken actual = state.SelectToken(condition["path"]?.ToString() ?? string.Empty, false);
            bool accepted = declared && actual != null &&
                CommandFieldContracts.ValidateCurrentState(state, condition, actual, out _);
            if (accepted)
            {
                throw new InvalidOperationException(name + " unexpectedly passed.");
            }
            Passed.Add(name);
        }
    }
}
