using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;

namespace OniAiProbeMod.Contracts
{
    internal sealed class CommandFieldContract
    {
        internal CommandFieldContract(
            string id,
            string source,
            string semantic,
            string unit,
            string scope,
            params string[] statusCompletenessPairs)
        {
            Id = id;
            Source = source;
            Semantic = semantic;
            Unit = unit;
            Scope = scope;
            StatusCompletenessPairs = new HashSet<string>(
                statusCompletenessPairs ?? Array.Empty<string>(),
                StringComparer.Ordinal);
        }

        internal string Id { get; }
        internal string Source { get; }
        internal string Semantic { get; }
        internal string Unit { get; }
        internal string Scope { get; }
        internal HashSet<string> StatusCompletenessPairs { get; }
        internal bool MutationConditionAllowed { get; set; } = true;
    }

    /// <summary>
    /// Trusted, executor-side contracts for fields allowed to authorize commands.
    /// The command only carries a contract id; it cannot redefine the contract.
    /// Unknown paths and metadata mismatches fail closed.
    /// </summary>
    internal static class CommandFieldContracts
    {
        internal const string GamePausedId = "oni.qc.game.paused.v1";
        internal const string GameBuildModeId = "oni.qc.game.build_mode.v1";
        internal const string ResearchCurrentId = "oni.qc.research.current.v1";
        internal const string ResearchCompleteId = "oni.qc.research.complete.v1";
        // Completed-prefab counts (buildings.oxygen_diffuser/outhouse/
        // manual_generator) are diagnostic only: the Bridge's rules and the
        // model loop's colony summary consume them and no command may bind
        // them, so TryResolve deliberately does not know their paths.  Their
        // capture binding is buildings.captured_at (the reader caches for
        // 30 s), which the snapshot.ts binding required here cannot express.
        internal const string BuildingCompletedCountId = "oni.qc.buildings.completed_prefab_count.v1";
        internal const string RegisteredFoodId = "oni.qc.resources.registered_food.v1";
        internal const string CanonicalAlertsId = "oni.qc.alerts.canonical.v1";
        internal const string ActiveWorldDuplicantCountId = "oni.qc.duplicants.active_world_count.v1";
        internal const string DuplicantIdleId = "oni.qc.duplicants.idle.v1";
        internal const string DuplicantStressedId = "oni.qc.duplicants.stressed.v1";
        internal const string DuplicantPriorityId = "oni.qc.duplicants.chore_priority.v1";
        internal const string DigToolId = "oni.qc.designations.dig_tool.v1";
        internal const string DigCellId = "oni.qc.designations.dig_cell.v1";
        internal const string BlueprintCatalogId = "oni.qc.blueprints.catalog.v1";
        internal const string BlueprintCellId = "oni.qc.blueprints.cell.v1";
        internal const string BlueprintInstanceId = "oni.qc.blueprints.instance.v1";
        internal const string UtilityPathId = "oni.qc.blueprints.utility_path.v1";

        private const int MaxCaptureAgeSeconds = 5;
        private const int MaxResearchIdLength = 128;
        // Db.Techs holds roughly 120 technologies; the bound only guards the wire size.
        private const int MaxCompletedTechIds = 512;
        internal const string ResearchCompleteSource = "Db.Techs+Research.Get+TechInstance.IsComplete";
        internal const string BuildingCompletedCountSource = "Components.BuildingCompletes.GetWorldItems(activeWorldId)";
        private const string FoodSource =
            "ClusterManager.activeWorld.worldInventory.GetPickupables(GameTags.Edible)+!GameTags.StoredPrivate";
        internal const string DuplicantIdleSource =
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+GameTags.Idle";
        internal const string DuplicantStressedSource =
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+StressMonitor";
        internal const string ActiveWorldDuplicantCountSource =
            "Components.LiveMinionIdentities.active_world";

        private static readonly CommandFieldContract GamePaused = new CommandFieldContract(
            GamePausedId,
            "SpeedControlScreen.IsPaused",
            "direct_state",
            "boolean",
            "active_game_session",
            "observed|complete");

        private static readonly CommandFieldContract GameBuildMode = new CommandFieldContract(
            GameBuildModeId,
            "Game.SandboxModeActive+DebugHandler.InstantBuildMode",
            "direct_state",
            "boolean",
            "active_game_session",
            "observed|complete");

        private static readonly CommandFieldContract ResearchCurrent = new CommandFieldContract(
            ResearchCurrentId,
            "Research.GetActiveResearch",
            "active_selection",
            "technology_id_or_empty",
            "save_research",
            "observed|complete");

        private static readonly CommandFieldContract ResearchComplete = new CommandFieldContract(
            ResearchCompleteId,
            ResearchCompleteSource,
            "completed_technology_ids",
            "technology_id_list",
            "save_research",
            "observed|complete")
        {
            // The finished set only informs candidate selection (a finished
            // tech is not offered); set_research binds research.current alone.
            MutationConditionAllowed = false
        };

        private static readonly CommandFieldContract RegisteredFood = new CommandFieldContract(
            RegisteredFoodId,
            FoodSource,
            "registered_non_private_inventory_sum",
            "kcal",
            "active_world_registered_non_private_edibles",
            "observed|complete",
            "estimated|bounded")
        {
            MutationConditionAllowed = false
        };

        private static readonly CommandFieldContract CanonicalAlerts = new CommandFieldContract(
            CanonicalAlertsId,
            CanonicalAlertReader.Source,
            "canonical_active_alert_ids",
            "alert_id_list",
            // Equals CanonicalAlertReader.Scope (CommandContractsHarness checks
            // it); a literal keeps this file compilable against reader stubs.
            "all_worlds",
            "observed|complete");

        private static readonly CommandFieldContract ActiveWorldDuplicantCount = new CommandFieldContract(
            ActiveWorldDuplicantCountId,
            ActiveWorldDuplicantCountSource,
            "live_count",
            "count",
            "active_world",
            "observed|complete");

        private static readonly CommandFieldContract DuplicantIdle = new CommandFieldContract(
            DuplicantIdleId,
            DuplicantIdleSource,
            "idle_count",
            "count",
            "active_world",
            "observed|complete")
        {
            MutationConditionAllowed = false
        };

        private static readonly CommandFieldContract DuplicantStressed = new CommandFieldContract(
            DuplicantStressedId,
            DuplicantStressedSource,
            "stressed_count",
            "count",
            "active_world",
            "observed|complete")
        {
            MutationConditionAllowed = false
        };

        private static readonly CommandFieldContract DuplicantPriority = new CommandFieldContract(
            DuplicantPriorityId,
            "Components.LiveMinionIdentities+ChoreConsumer",
            "configured_chore_priority",
            "priority_0_to_5",
            "active_world_live_duplicant",
            "observed|complete");

        private static readonly CommandFieldContract DigTool = new CommandFieldContract(
            DigToolId,
            "Grid+DigTool+Components.Diggables",
            "dig_tool_filter_state",
            "boolean",
            "active_game_session",
            "observed|complete");

        private static readonly CommandFieldContract DigCell = new CommandFieldContract(
            DigCellId,
            "Grid+DigTool+Components.Diggables",
            "dig_cell_state_field",
            "field_native",
            "active_world_tracked_cells",
            "observed|complete_for_tracked_cell");

        private static readonly CommandFieldContract BlueprintCatalog = new CommandFieldContract(
            BlueprintCatalogId,
            "BuildingDef+Grid+Constructable+WorldInventory",
            "blueprint_catalog_field",
            "field_native",
            "approved_blueprint_catalog",
            "observed|complete");

        private static readonly CommandFieldContract BlueprintCell = new CommandFieldContract(
            BlueprintCellId,
            "BuildingDef+Grid+Constructable+WorldInventory",
            "blueprint_cell_state_field",
            "field_native",
            "active_world_tracked_cells",
            "observed|complete_for_tracked_cell");

        private static readonly CommandFieldContract BlueprintInstance = new CommandFieldContract(
            BlueprintInstanceId,
            "BuildingDef+Grid+Constructable+WorldInventory",
            "blueprint_instance_state_field",
            "field_native",
            "active_world_registered_blueprints",
            "observed|complete_for_registered_instance");

        private static readonly CommandFieldContract UtilityPath = new CommandFieldContract(
            UtilityPathId,
            "BuildingDef+Grid+Constructable+WorldInventory",
            "utility_blueprint_path_state_field",
            "field_native",
            "probe_tracked_utility_paths",
            "observed|complete_for_tracked_path");

        private static readonly Regex TechIdPattern = new Regex(
            @"^[A-Za-z0-9_]{1,128}$",
            RegexOptions.CultureInvariant);

        private static readonly Regex DuplicantPriorityPath = new Regex(
            @"^duplicants\.by_instance_id\.id_[1-9][0-9]*\.chore_priorities\.[A-Za-z0-9_]{1,64}$",
            RegexOptions.CultureInvariant);

        private static readonly Regex DigCellPath = new Regex(
            @"^designations\.dig\.by_cell\.cell_[0-9]+\.(cell|world_id|solid|element_id|mass_kg|damage|designation_present|designation_instance_id|designation_kind|designation_in_progress|safe_mark_candidate|fingerprint)$",
            RegexOptions.CultureInvariant);

        private static readonly Regex BlueprintCatalogPath = new Regex(
            @"^blueprints\.catalog\.(Ladder|Wire|MineralDeoxidizer|AlgaeHabitat)\.(available|prefab_id|is_utility|cancellable|orientation|facade_id|material_tags)$",
            RegexOptions.CultureInvariant);

        private static readonly Regex BlueprintCellPath = new Regex(
            @"^cells\.by_id\.cell_[0-9]+\.(cell|x|y|world_id|visible|solid|foundation|element_id|mass_kg|damage|object_count|empty_for_blueprint|can_place_ladder|can_place_wire|can_place_mineral_deoxidizer|can_place_algae_habitat|blueprint_present|blueprint_instance_id|blueprint_prefab_id|blueprint_orientation|blueprint_material_tags|blueprint_in_progress|blueprint_is_utility|blueprint_progress_percent|blueprint_worker_instance_id|blueprint_root_cell|blueprint_footprint_cells|blueprint_priority_class|blueprint_priority_value|empty_fingerprint|fingerprint)$",
            RegexOptions.CultureInvariant);

        private static readonly Regex BlueprintInstancePath = new Regex(
            @"^blueprints\.by_instance_id\.id_[1-9][0-9]*\.(instance_id|cell|world_id|prefab_id|orientation|material_tags|in_progress|is_utility|fingerprint|progress_percent|worker_instance_id|root_cell|footprint_cells|priority_class|priority_value)$",
            RegexOptions.CultureInvariant);

        private static readonly Regex UtilityPathPath = new Regex(
            @"^blueprints\.utility_paths\.id_[A-Za-z0-9_]{1,64}\.(path_id|building_def_id|cells|blueprint_instance_ids|active_blueprint_instance_ids|active|fully_active)$",
            RegexOptions.CultureInvariant);

        internal static bool TryResolve(string path, out CommandFieldContract contract)
        {
            switch (path)
            {
                case "game.paused":
                    contract = GamePaused;
                    return true;
                case "game.sandbox_mode":
                case "game.instant_build_mode":
                    contract = GameBuildMode;
                    return true;
                case "research.current":
                    contract = ResearchCurrent;
                    return true;
                case "research.complete":
                    contract = ResearchComplete;
                    return true;
                case "resources.food_kcal":
                    contract = RegisteredFood;
                    return true;
                case "alerts":
                    contract = CanonicalAlerts;
                    return true;
                case "duplicants.total":
                    contract = ActiveWorldDuplicantCount;
                    return true;
                case "duplicants.idle":
                    contract = DuplicantIdle;
                    return true;
                case "duplicants.stressed":
                    contract = DuplicantStressed;
                    return true;
                case "designations.dig.tool.tiles_filter_on":
                case "designations.dig.tool.natural_backwall_filter_on":
                    contract = DigTool;
                    return true;
            }

            if (DuplicantPriorityPath.IsMatch(path))
            {
                contract = DuplicantPriority;
                return true;
            }
            if (DigCellPath.IsMatch(path))
            {
                contract = DigCell;
                return true;
            }
            if (BlueprintCatalogPath.IsMatch(path))
            {
                contract = BlueprintCatalog;
                return true;
            }
            if (BlueprintCellPath.IsMatch(path))
            {
                contract = BlueprintCell;
                return true;
            }
            if (BlueprintInstancePath.IsMatch(path))
            {
                contract = BlueprintInstance;
                return true;
            }
            if (UtilityPathPath.IsMatch(path))
            {
                contract = UtilityPath;
                return true;
            }

            contract = null;
            return false;
        }

        internal static bool DeclarationMatches(JObject condition, out string error)
        {
            string path = condition?["path"]?.Type == JTokenType.String
                ? condition["path"].ToString()
                : null;
            if (string.IsNullOrEmpty(path) || !TryResolve(path, out CommandFieldContract contract))
            {
                error = (path ?? "<missing>") + " has no trusted command field contract.";
                return false;
            }

            string declared = condition["quality_contract_id"]?.Type == JTokenType.String
                ? condition["quality_contract_id"].ToString()
                : null;
            if (!string.Equals(declared, contract.Id, StringComparison.Ordinal))
            {
                error = path + " must declare trusted quality_contract_id " + contract.Id + ".";
                return false;
            }

            if (!(condition["quality"] is JArray qualities) || qualities.Count == 0)
            {
                error = path + " quality allowance must be a non-empty string array.";
                return false;
            }
            HashSet<string> seenQualities = new HashSet<string>(StringComparer.Ordinal);
            foreach (JToken quality in qualities)
            {
                if (quality.Type != JTokenType.String ||
                    !seenQualities.Add(quality.ToString()) ||
                    !ContractAllowsStatus(contract, quality.ToString()))
                {
                    error = path + " quality allowance is invalid for " + contract.Id + ".";
                    return false;
                }
            }

            string op = condition["op"]?.Type == JTokenType.String
                ? condition["op"].ToString()
                : null;
            JToken expected = condition["value"];
            if (!ConditionOperandMatches(path, op, expected))
            {
                error = path + " operator/value is invalid for " + contract.Id + ".";
                return false;
            }

            error = null;
            return true;
        }

        internal static bool MutationEligible(JObject condition, out string error)
        {
            string path = condition?["path"]?.Type == JTokenType.String
                ? condition["path"].ToString()
                : null;
            if (string.IsNullOrEmpty(path) || !TryResolve(path, out CommandFieldContract contract))
            {
                error = (path ?? "<missing>") + " has no trusted command field contract.";
                return false;
            }

            // Diagnostic-only fields may authorize non-mutating conditions,
            // but must never authorize a state-changing precondition/effect.
            if (!contract.MutationConditionAllowed)
            {
                error = path + " contract " + contract.Id + " is not mutation-eligible.";
                return false;
            }

            error = null;
            return true;
        }

        internal static bool ValidateCurrentState(
            JObject state,
            JObject condition,
            JToken actual,
            out string error)
        {
            if (!DeclarationMatches(condition, out error))
            {
                return false;
            }

            string path = condition["path"].ToString();
            if (!TryResolve(path, out CommandFieldContract contract))
            {
                error = (path ?? "<missing>") + " has no trusted command field contract.";
                return false;
            }

            JObject quality = state?["quality"]?[path] as JObject;
            if (quality == null)
            {
                error = path + " quality entry is missing.";
                return false;
            }

            string status = RequiredString(quality, "status");
            string completeness = RequiredString(quality, "completeness");
            if (!contract.StatusCompletenessPairs.Contains(status + "|" + completeness) ||
                !string.Equals(RequiredString(quality, "source"), contract.Source, StringComparison.Ordinal) ||
                !string.Equals(RequiredString(quality, "semantic"), contract.Semantic, StringComparison.Ordinal) ||
                !string.Equals(RequiredString(quality, "unit"), contract.Unit, StringComparison.Ordinal) ||
                !string.Equals(RequiredString(quality, "scope"), contract.Scope, StringComparison.Ordinal) ||
                !string.Equals(RequiredString(quality, "captured_at"), "snapshot.ts", StringComparison.Ordinal) ||
                !string.Equals(RequiredString(quality, "contract_id"), contract.Id, StringComparison.Ordinal))
            {
                error = path + " quality metadata does not match " + contract.Id + ".";
                return false;
            }

            if (!TryValidateCapture(state, out string captureError))
            {
                error = path + " " + captureError;
                return false;
            }

            if (!ValueMatchesPath(path, actual))
            {
                error = path + " value has an invalid wire type or range for " + contract.Id + ".";
                return false;
            }

            error = null;
            return true;
        }

        private static bool TryValidateCapture(JObject state, out string error)
        {
            string timestamp = state?["ts"]?.Type == JTokenType.String ? state["ts"].ToString() : null;
            if (string.IsNullOrEmpty(timestamp) ||
                !DateTimeOffset.TryParse(
                    timestamp,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind,
                    out DateTimeOffset capturedAt))
            {
                error = "capture binding snapshot.ts is missing or invalid.";
                return false;
            }

            TimeSpan age = DateTimeOffset.Now - capturedAt;
            if (age < TimeSpan.FromSeconds(-MaxCaptureAgeSeconds) ||
                age > TimeSpan.FromSeconds(MaxCaptureAgeSeconds))
            {
                error = "capture binding snapshot.ts is outside the executor freshness window.";
                return false;
            }

            error = null;
            return true;
        }

        private static bool ValueMatchesPath(string path, JToken value)
        {
            if (value == null)
            {
                return false;
            }
            if (path == "game.paused" || path == "game.sandbox_mode" || path == "game.instant_build_mode")
            {
                return value.Type == JTokenType.Boolean;
            }
            if (path == "research.current")
            {
                return value.Type == JTokenType.String && value.ToString().Length <= MaxResearchIdLength;
            }
            if (path == "research.complete")
            {
                return IsSortedTechIdArray(value);
            }
            if (path == "resources.food_kcal")
            {
                return IsFiniteNumber(value, minimum: 0d, maximum: 1_000_000_000_000d);
            }
            if (path == "duplicants.total" ||
                path == "duplicants.idle" ||
                path == "duplicants.stressed")
            {
                return IsIntegerInRange(value, 0, 1000);
            }
            if (path == "alerts")
            {
                if (!(value is JArray alerts) || alerts.Count > CanonicalAlertCatalog.MaximumAlertCount)
                {
                    return false;
                }
                HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
                string previous = null;
                foreach (JToken alert in alerts)
                {
                    string current = alert.Type == JTokenType.String ? alert.ToString() : null;
                    if (current == null ||
                        !Regex.IsMatch(current, @"^[a-z][a-z0-9_]{0,63}$", RegexOptions.CultureInvariant) ||
                        !CanonicalAlertCatalog.IsKnown(current) ||
                        !seen.Add(current) ||
                        (previous != null && string.CompareOrdinal(previous, current) >= 0))
                    {
                        return false;
                    }
                    previous = current;
                }
                return true;
            }
            if (DuplicantPriorityPath.IsMatch(path))
            {
                return IsIntegerInRange(value, 0, 5);
            }
            if (path.StartsWith("designations.dig.tool.", StringComparison.Ordinal))
            {
                return value.Type == JTokenType.Boolean;
            }
            if (DigCellPath.IsMatch(path) ||
                BlueprintCatalogPath.IsMatch(path) ||
                BlueprintCellPath.IsMatch(path) ||
                BlueprintInstancePath.IsMatch(path) ||
                UtilityPathPath.IsMatch(path))
            {
                return DynamicValueMatches(path, value);
            }
            return false;
        }

        private static bool DynamicValueMatches(string path, JToken value)
        {
            string field = path.Substring(path.LastIndexOf('.') + 1);
            switch (field)
            {
                case "available":
                case "is_utility":
                case "cancellable":
                case "visible":
                case "solid":
                case "foundation":
                case "empty_for_blueprint":
                case "can_place_ladder":
                case "can_place_wire":
                case "can_place_mineral_deoxidizer":
                case "can_place_algae_habitat":
                case "blueprint_present":
                case "blueprint_in_progress":
                case "blueprint_is_utility":
                case "in_progress":
                case "active":
                case "fully_active":
                case "designation_present":
                case "designation_in_progress":
                case "safe_mark_candidate":
                    return value.Type == JTokenType.Boolean;
                case "cell":
                case "x":
                case "y":
                case "world_id":
                case "object_count":
                case "blueprint_instance_id":
                case "instance_id":
                case "designation_instance_id":
                case "blueprint_worker_instance_id":
                case "worker_instance_id":
                case "blueprint_root_cell":
                case "root_cell":
                    return IsIntegerInRange(value, 0, int.MaxValue);
                case "blueprint_progress_percent":
                case "progress_percent":
                    return IsIntegerInRange(value, 0, 100);
                case "blueprint_priority_value":
                case "priority_value":
                    return IsIntegerInRange(value, 1, 9);
                case "blueprint_priority_class":
                case "priority_class":
                    return IsPriorityClass(value);
                case "mass_kg":
                case "damage":
                    return IsFiniteNumber(value, minimum: 0d);
                case "fingerprint":
                case "empty_fingerprint":
                    return value.Type == JTokenType.String &&
                        Regex.IsMatch(value.ToString(), @"^sha256:[0-9a-f]{64}$", RegexOptions.CultureInvariant);
                case "material_tags":
                case "blueprint_material_tags":
                    return IsBoundedStringArray(value, 8, 64);
                case "cells":
                case "blueprint_instance_ids":
                case "active_blueprint_instance_ids":
                    return IsBoundedIntegerArray(value, 64);
                case "blueprint_footprint_cells":
                case "footprint_cells":
                    return IsBlueprintFootprint(value);
                case "element_id":
                case "designation_kind":
                case "prefab_id":
                case "orientation":
                case "facade_id":
                case "blueprint_prefab_id":
                case "blueprint_orientation":
                case "path_id":
                case "building_def_id":
                    return value.Type == JTokenType.String && value.ToString().Length <= 128;
                default:
                    return false;
            }
        }

        // Ordinal strictly ascending: sorted and free of duplicates in one pass.
        private static bool IsSortedTechIdArray(JToken value)
        {
            if (!(value is JArray items) || items.Count > MaxCompletedTechIds)
            {
                return false;
            }
            string previous = null;
            foreach (JToken item in items)
            {
                string current = item.Type == JTokenType.String ? item.ToString() : null;
                if (current == null ||
                    !TechIdPattern.IsMatch(current) ||
                    (previous != null && string.CompareOrdinal(previous, current) >= 0))
                {
                    return false;
                }
                previous = current;
            }
            return true;
        }

        private static bool ContractAllowsStatus(CommandFieldContract contract, string status)
        {
            foreach (string pair in contract.StatusCompletenessPairs)
            {
                if (pair.StartsWith(status + "|", StringComparison.Ordinal))
                {
                    return true;
                }
            }
            return false;
        }

        private static bool ConditionOperandMatches(string path, string op, JToken value)
        {
            if (op == "eq" || op == "ne")
            {
                return ValueMatchesPath(path, value);
            }
            if (op == "lt" || op == "lte" || op == "gt" || op == "gte")
            {
                return IsOrderedNumericPath(path) && ValueMatchesPath(path, value);
            }
            if (op != "contains")
            {
                return false;
            }

            if (path == "alerts")
            {
                return value?.Type == JTokenType.String &&
                    Regex.IsMatch(value.ToString(), @"^[a-z][a-z0-9_]{0,63}$", RegexOptions.CultureInvariant) &&
                    CanonicalAlertCatalog.IsKnown(value.ToString());
            }
            if (path == "research.complete")
            {
                return value?.Type == JTokenType.String && TechIdPattern.IsMatch(value.ToString());
            }

            string field = path.Substring(path.LastIndexOf('.') + 1);
            if (field == "material_tags" || field == "blueprint_material_tags")
            {
                return value?.Type == JTokenType.String && value.ToString().Length <= 64;
            }
            if (field == "cells" ||
                field == "blueprint_instance_ids" ||
                field == "active_blueprint_instance_ids" ||
                field == "blueprint_footprint_cells" ||
                field == "footprint_cells")
            {
                return IsIntegerInRange(value, 0, int.MaxValue);
            }
            return false;
        }

        private static bool IsOrderedNumericPath(string path)
        {
            if (path == "resources.food_kcal" ||
                path == "duplicants.total" ||
                path == "duplicants.idle" ||
                path == "duplicants.stressed" ||
                DuplicantPriorityPath.IsMatch(path))
            {
                return true;
            }

            if (!(DigCellPath.IsMatch(path) ||
                BlueprintCatalogPath.IsMatch(path) ||
                BlueprintCellPath.IsMatch(path) ||
                BlueprintInstancePath.IsMatch(path) ||
                UtilityPathPath.IsMatch(path)))
            {
                return false;
            }

            string field = path.Substring(path.LastIndexOf('.') + 1);
            switch (field)
            {
                case "cell":
                case "x":
                case "y":
                case "world_id":
                case "object_count":
                case "blueprint_instance_id":
                case "instance_id":
                case "designation_instance_id":
                case "blueprint_progress_percent":
                case "progress_percent":
                case "blueprint_worker_instance_id":
                case "worker_instance_id":
                case "blueprint_root_cell":
                case "root_cell":
                case "blueprint_priority_value":
                case "priority_value":
                case "mass_kg":
                case "damage":
                    return true;
                default:
                    return false;
            }
        }

        private static bool IsFiniteNumber(
            JToken value,
            double minimum,
            double maximum = double.PositiveInfinity)
        {
            if (value.Type != JTokenType.Integer && value.Type != JTokenType.Float)
            {
                return false;
            }
            double number = value.Value<double>();
            return !double.IsNaN(number) && !double.IsInfinity(number) &&
                number >= minimum && number <= maximum;
        }

        private static bool IsIntegerInRange(JToken value, int minimum, int maximum)
        {
            if (value == null || value.Type != JTokenType.Integer)
            {
                return false;
            }
            long number = value.Value<long>();
            return number >= minimum && number <= maximum;
        }

        private static bool IsPriorityClass(JToken value)
        {
            if (value.Type != JTokenType.String)
            {
                return false;
            }
            switch (value.ToString())
            {
                case "idle":
                case "basic":
                case "high":
                case "personalNeeds":
                case "topPriority":
                case "compulsory":
                    return true;
                default:
                    return false;
            }
        }

        private static bool IsBlueprintFootprint(JToken value)
        {
            if (!(value is JArray array) || array.Count < 1 || array.Count > 64)
            {
                return false;
            }
            int previous = -1;
            foreach (JToken cell in array)
            {
                if (!IsIntegerInRange(cell, 0, int.MaxValue))
                {
                    return false;
                }
                int current = cell.Value<int>();
                // Strict ordering proves both canonical order and uniqueness.
                if (current <= previous)
                {
                    return false;
                }
                previous = current;
            }
            return true;
        }

        private static bool IsBoundedStringArray(JToken value, int maximumCount, int maximumLength)
        {
            if (!(value is JArray array) || array.Count > maximumCount)
            {
                return false;
            }
            foreach (JToken item in array)
            {
                if (item.Type != JTokenType.String || item.ToString().Length > maximumLength)
                {
                    return false;
                }
            }
            return true;
        }

        private static bool IsBoundedIntegerArray(JToken value, int maximumCount)
        {
            if (!(value is JArray array) || array.Count > maximumCount)
            {
                return false;
            }
            foreach (JToken item in array)
            {
                if (!IsIntegerInRange(item, 0, int.MaxValue))
                {
                    return false;
                }
            }
            return true;
        }

        private static string RequiredString(JObject value, string name)
        {
            return value?[name]?.Type == JTokenType.String ? value[name].ToString() : null;
        }
    }
}
