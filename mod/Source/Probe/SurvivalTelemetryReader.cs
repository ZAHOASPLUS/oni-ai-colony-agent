using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Raw, provenance-labelled survival telemetry.
    ///
    /// This reader intentionally does not manufacture policy conclusions such
    /// as "critical load covered", "N-1 safe", or "sustainable".  Those
    /// conclusions require a continuous evidence window and a versioned policy
    /// outside the game probe.  The probe only publishes gauges, counts, and
    /// explicit unsupported markers here.
    /// </summary>
    internal static class SurvivalTelemetryReader
    {
        private const int ActiveWorldCellScanLimit = 500000;
        internal const int ObservationScopeIdentityEnumerationLimit = 1000;
        internal const int PowerRegistryEnumerationLimit = 100000;
        internal const int ActiveWorldCellScanCellBudget = 4096;
        internal const double ActiveWorldCellScanTimeBudgetMs = 4.0;
        internal const double ActiveWorldCellAggregateMaxAgeSeconds = 900.0;
        private static ActiveWorldCellScanState activeWorldCellScan;
        private static ActiveWorldCellScanState lastCompletedActiveWorldCellScan;
        private static long activeWorldCellScanGeneration;

        internal static void ResetSession()
        {
            ResetActiveWorldCellScans();
            activeWorldCellScanGeneration = 0;
        }

        private static void ResetActiveWorldCellScans()
        {
            activeWorldCellScan = null;
            lastCompletedActiveWorldCellScan = null;
        }

        internal static SurvivalTelemetryReadResult Read()
        {
            SurvivalTelemetryReadResult result = new SurvivalTelemetryReadResult();
            SeedUnsupportedDecisionFields(result);

            ClusterManager manager = null;
            WorldContainer activeWorld = null;
            try
            {
                manager = ClusterManager.Instance;
                activeWorld = manager?.activeWorld;
            }
            catch
            {
                // Individual readers retain unsupported/error quality below.
            }

            ReadObservationScope(result, manager, activeWorld);
            ReadActiveWorldCells(result, activeWorld);
            ReadPower(result, activeWorld);
            ReadRockets(result);
            return result;
        }

        private static void ReadObservationScope(
            SurvivalTelemetryReadResult result,
            ClusterManager manager,
            WorldContainer activeWorld)
        {
            const string source = "Components.LiveMinionIdentities+ClusterManager.activeWorld";
            HashSet<int> populatedWorldIdSet = new HashSet<int>();
            List<object> populatedJson = new List<object>();
            List<object> coveredJson = new List<object>();
            int activeWorldId = -1;
            bool coverageComplete = false;
            string status = "unsupported";

            try
            {
                if (manager != null && activeWorld != null)
                {
                    activeWorldId = manager.activeWorldId;
                    if (activeWorldId >= 0)
                    {
                        if (!ObservationScopeWorldBindingMatches(manager, activeWorld, activeWorldId))
                        {
                            throw new InvalidOperationException("observation_scope_active_world_binding_invalid");
                        }

                        IEnumerable<MinionIdentity> identities = Components.LiveMinionIdentities.Items;
                        if (identities == null)
                        {
                            throw new InvalidOperationException("observation_scope_registry_unavailable");
                        }

                        int enumeratedIdentityCount = 0;
                        foreach (MinionIdentity identity in identities)
                        {
                            if (enumeratedIdentityCount >= ObservationScopeIdentityEnumerationLimit)
                            {
                                throw new InvalidOperationException("observation_scope_identity_limit_exceeded");
                            }
                            enumeratedIdentityCount++;

                            if (identity == null)
                            {
                                throw new InvalidOperationException("observation_scope_identity_invalid");
                            }

                            int worldId = identity.GetMyWorldId();
                            if (worldId < 0)
                            {
                                throw new InvalidOperationException("observation_scope_world_id_invalid");
                            }
                            populatedWorldIdSet.Add(worldId);
                        }

                        if (!ObservationScopeWorldBindingMatches(manager, activeWorld, activeWorldId))
                        {
                            throw new InvalidOperationException("observation_scope_active_world_changed");
                        }

                        List<int> populatedWorldIds = new List<int>(populatedWorldIdSet);
                        populatedWorldIds.Sort();
                        foreach (int worldId in populatedWorldIds)
                        {
                            populatedJson.Add(worldId);
                        }

                        // v1 intentionally covers exactly one world. A save
                        // with duplicants on any other world remains observable
                        // but can never claim complete coverage.
                        coveredJson.Add(activeWorldId);
                        coverageComplete = populatedWorldIds.Count == 1 &&
                            populatedWorldIds[0] == activeWorldId;
                        status = "observed";
                    }
                }
            }
            catch
            {
                populatedJson.Clear();
                coveredJson.Clear();
                coverageComplete = false;
                status = "error";
            }

            result.ObservationScope["active_world_id"] = activeWorldId >= 0 ? (object)activeWorldId : null;
            result.ObservationScope["populated_world_ids"] = populatedJson;
            result.ObservationScope["covered_world_ids"] = coveredJson;
            result.ObservationScope["complete_for_populated_worlds"] = coverageComplete;
            result.Quality["observation_scope.active_world_id"] = Quality(
                status,
                source,
                "raw_identifier",
                "world_id",
                "active_world",
                status == "observed" ? "complete" : "unknown");
            result.Quality["observation_scope.populated_world_ids"] = Quality(
                status,
                source,
                "raw_set",
                "world_id",
                "save",
                status == "observed" ? "complete" : "unknown");
            result.Quality["observation_scope.covered_world_ids"] = Quality(
                status,
                source,
                "raw_set",
                "world_id",
                "snapshot",
                status == "observed" ? "complete" : "unknown");
            result.Quality["observation_scope.complete_for_populated_worlds"] = Quality(
                status,
                source,
                "raw_coverage",
                "boolean",
                "save",
                status == "observed" ? "complete" : "unknown");
        }

        private static bool ObservationScopeWorldBindingMatches(
            ClusterManager manager,
            WorldContainer activeWorld,
            int activeWorldId)
        {
            return manager != null &&
                activeWorld != null &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId &&
                activeWorld.id == activeWorldId;
        }

        private static void ReadActiveWorldCells(
            SurvivalTelemetryReadResult result,
            WorldContainer activeWorld)
        {
            const string source = "WorldContainer.WorldOffset+WorldSize-HiddenYOffset+Grid.IsVisible+Grid.Element+Grid.Mass+Grid.Temperature";
            Dictionary<string, object> cells = new Dictionary<string, object>
            {
                { "clean_oxygen_kg", null },
                { "polluted_oxygen_kg", null },
                { "clean_water_kg", null },
                { "polluted_water_kg", null },
                { "salt_water_kg", null },
                { "brine_kg", null },
                { "valid_cell_count", null },
                { "candidate_cell_count", null },
                { "scanned_candidate_cell_count", null },
                { "remaining_candidate_cell_count", null },
                { "scanned_valid_cell_count", null },
                { "scan_progress_ratio", 0.0 },
                { "scan_generation", null },
                { "scan_scope", "active_world_visible_cells" },
                { "scan_cell_budget", ActiveWorldCellScanCellBudget },
                { "scan_time_budget_ms", ActiveWorldCellScanTimeBudgetMs },
                { "scan_call_processed_cell_count", 0 },
                { "scan_window_started_at", null },
                { "scan_window_completed_at", null },
                { "scan_status", "unavailable" },
                { "scan_complete", false },
                { "aggregate_available", false },
                { "aggregate_scan_generation", null },
                { "aggregate_window_started_at", null },
                { "aggregate_window_completed_at", null },
                { "aggregate_age_seconds", null },
                { "aggregate_max_age_seconds", ActiveWorldCellAggregateMaxAgeSeconds }
            };
            result.Materials["active_world_cells"] = cells;
            result.Temperature["active_world_material_min_c"] = null;
            result.Temperature["active_world_material_max_c"] = null;

            if (activeWorld == null)
            {
                ResetActiveWorldCellScans();
                AddCellAggregateQuality(result, "unsupported", source, "unknown", false);
                AddCellProgressQuality(result, "unsupported", source, "unknown");
                return;
            }

            int width = activeWorld.Width;
            int height = Math.Max(0, activeWorld.Height - activeWorld.HiddenYOffset);
            long candidateCount = (long)width * height;
            if (width <= 0 || height <= 0 || candidateCount > ActiveWorldCellScanLimit)
            {
                ResetActiveWorldCellScans();
                cells["candidate_cell_count"] = candidateCount;
                cells["scan_status"] = "bounded";
                AddCellAggregateQuality(result, "unsupported", source, "bounded", false);
                AddCellProgressQuality(result, "unsupported", source, "bounded");
                return;
            }

            Vector2I offset = activeWorld.WorldOffset;
            if ((activeWorldCellScan != null &&
                    !activeWorldCellScan.Matches(activeWorld.id, offset.x, offset.y, width, height)) ||
                (lastCompletedActiveWorldCellScan != null &&
                    !lastCompletedActiveWorldCellScan.Matches(activeWorld.id, offset.x, offset.y, width, height)))
            {
                ResetActiveWorldCellScans();
            }

            if (activeWorldCellScan == null || activeWorldCellScan.Complete)
            {
                activeWorldCellScan = new ActiveWorldCellScanState(
                    ++activeWorldCellScanGeneration,
                    activeWorld.id,
                    offset.x,
                    offset.y,
                    width,
                    height,
                    (int)candidateCount);
            }

            ActiveWorldCellScanState scan = activeWorldCellScan;
            Stopwatch budget = Stopwatch.StartNew();
            int processedCellCount = 0;
            try
            {
                while (scan.NextCandidateIndex < scan.CandidateCellCount &&
                    processedCellCount < ActiveWorldCellScanCellBudget &&
                    budget.Elapsed.TotalMilliseconds < ActiveWorldCellScanTimeBudgetMs)
                {
                    int index = scan.NextCandidateIndex++;
                    processedCellCount++;
                    int localX = index % scan.Width;
                    int localY = index / scan.Width;
                    int cell = Grid.XYToCell(scan.OffsetX + localX, scan.OffsetY + localY);
                    if (!Grid.IsValidCellInWorld(cell, scan.WorldId))
                    {
                        continue;
                    }
                    if (!Grid.IsVisible(cell))
                    {
                        continue;
                    }

                    scan.ScannedValidCellCount++;
                    Element element = Grid.Element[cell];
                    float mass = Grid.Mass[cell];
                    if (element != null && !float.IsNaN(mass) && !float.IsInfinity(mass) && mass > 0f)
                    {
                        switch (element.id)
                        {
                            case SimHashes.Oxygen:
                                scan.CleanOxygen += mass;
                                break;
                            case SimHashes.ContaminatedOxygen:
                                scan.PollutedOxygen += mass;
                                break;
                            case SimHashes.Water:
                                scan.CleanWater += mass;
                                break;
                            case SimHashes.DirtyWater:
                                scan.PollutedWater += mass;
                                break;
                            case SimHashes.SaltWater:
                                scan.SaltWater += mass;
                                break;
                            case SimHashes.Brine:
                                scan.Brine += mass;
                                break;
                        }

                        // A valid grid cell can still be vacuum.  Temperature
                        // extrema labelled as material telemetry must only
                        // include cells that actually contain positive mass.
                        float kelvin = Grid.Temperature[cell];
                        if (!float.IsNaN(kelvin) && !float.IsInfinity(kelvin) && kelvin > 0f)
                        {
                            scan.MinimumKelvin = Math.Min(scan.MinimumKelvin, kelvin);
                            scan.MaximumKelvin = Math.Max(scan.MaximumKelvin, kelvin);
                        }
                    }
                }

                budget.Stop();
                if (scan.NextCandidateIndex >= scan.CandidateCellCount)
                {
                    scan.Complete = true;
                    scan.CompletedAt = DateTimeOffset.UtcNow;
                    lastCompletedActiveWorldCellScan = scan;
                }

                PopulateCellScanProgress(cells, scan, processedCellCount);
                bool aggregateTemperatureAvailable = PopulateCompletedCellAggregate(
                    result,
                    cells,
                    lastCompletedActiveWorldCellScan,
                    DateTimeOffset.UtcNow,
                    out bool aggregateFresh);
                bool aggregateAvailable = lastCompletedActiveWorldCellScan != null;

                AddCellAggregateQuality(
                    result,
                    aggregateAvailable
                        ? (aggregateFresh ? "estimated" : "stale")
                        : "unsupported",
                    source,
                    aggregateAvailable ? "complete" : "partial",
                    aggregateTemperatureAvailable);
                AddCellProgressQuality(result, "observed", source, "complete");
            }
            catch
            {
                budget.Stop();
                long failedGeneration = scan.Generation;
                ResetActiveWorldCellScans();
                cells["candidate_cell_count"] = scan.CandidateCellCount;
                cells["scanned_candidate_cell_count"] = scan.NextCandidateIndex;
                cells["remaining_candidate_cell_count"] = Math.Max(
                    0,
                    scan.CandidateCellCount - scan.NextCandidateIndex);
                cells["scanned_valid_cell_count"] = scan.ScannedValidCellCount;
                cells["scan_progress_ratio"] = scan.CandidateCellCount == 0
                    ? 0.0
                    : Math.Round((double)scan.NextCandidateIndex / scan.CandidateCellCount, 6);
                cells["scan_generation"] = failedGeneration;
                cells["scan_call_processed_cell_count"] = processedCellCount;
                cells["scan_window_started_at"] = scan.StartedAt.ToString("o", CultureInfo.InvariantCulture);
                cells["scan_status"] = "error";
                cells["scan_complete"] = false;
                AddCellAggregateQuality(result, "error", source, "unknown", false);
                AddCellProgressQuality(result, "error", source, "unknown");
            }
        }

        private static void PopulateCellScanProgress(
            Dictionary<string, object> cells,
            ActiveWorldCellScanState scan,
            int processedCellCount)
        {
            int scanned = scan.NextCandidateIndex;
            int remaining = Math.Max(0, scan.CandidateCellCount - scanned);
            cells["candidate_cell_count"] = scan.CandidateCellCount;
            cells["scanned_candidate_cell_count"] = scanned;
            cells["remaining_candidate_cell_count"] = remaining;
            cells["scanned_valid_cell_count"] = scan.ScannedValidCellCount;
            cells["scan_progress_ratio"] = scan.CandidateCellCount == 0
                ? 0.0
                : Math.Round((double)scanned / scan.CandidateCellCount, 6);
            cells["scan_generation"] = scan.Generation;
            cells["scan_call_processed_cell_count"] = processedCellCount;
            cells["scan_window_started_at"] = scan.StartedAt.ToString("o", CultureInfo.InvariantCulture);
            cells["scan_window_completed_at"] = scan.CompletedAt.HasValue
                ? (object)scan.CompletedAt.Value.ToString("o", CultureInfo.InvariantCulture)
                : null;
            cells["scan_status"] = scan.Complete ? "complete" : "partial";
            cells["scan_complete"] = scan.Complete;
        }

        private static bool PopulateCompletedCellAggregate(
            SurvivalTelemetryReadResult result,
            Dictionary<string, object> cells,
            ActiveWorldCellScanState completed,
            DateTimeOffset observedAt,
            out bool aggregateFresh)
        {
            aggregateFresh = false;
            if (completed == null || !completed.Complete || !completed.CompletedAt.HasValue)
            {
                return false;
            }

            cells["clean_oxygen_kg"] = RoundMass(completed.CleanOxygen);
            cells["polluted_oxygen_kg"] = RoundMass(completed.PollutedOxygen);
            cells["clean_water_kg"] = RoundMass(completed.CleanWater);
            cells["polluted_water_kg"] = RoundMass(completed.PollutedWater);
            cells["salt_water_kg"] = RoundMass(completed.SaltWater);
            cells["brine_kg"] = RoundMass(completed.Brine);
            cells["valid_cell_count"] = completed.ScannedValidCellCount;
            cells["aggregate_available"] = true;
            cells["aggregate_scan_generation"] = completed.Generation;
            cells["aggregate_window_started_at"] = completed.StartedAt.ToString("o", CultureInfo.InvariantCulture);
            cells["aggregate_window_completed_at"] = completed.CompletedAt.Value.ToString("o", CultureInfo.InvariantCulture);
            double aggregateAgeSeconds = Math.Max(
                0.0,
                (observedAt - completed.CompletedAt.Value).TotalSeconds);
            cells["aggregate_age_seconds"] = Math.Round(aggregateAgeSeconds, 3);
            aggregateFresh = aggregateAgeSeconds <= ActiveWorldCellAggregateMaxAgeSeconds;

            if (double.IsPositiveInfinity(completed.MinimumKelvin) ||
                double.IsNegativeInfinity(completed.MaximumKelvin))
            {
                return false;
            }

            result.Temperature["active_world_material_min_c"] = Math.Round(completed.MinimumKelvin - 273.15, 3);
            result.Temperature["active_world_material_max_c"] = Math.Round(completed.MaximumKelvin - 273.15, 3);
            return true;
        }

        private static void AddCellAggregateQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness,
            bool temperatureAvailable)
        {
            const string aggregateCapturedAt = "materials.active_world_cells.aggregate_window_completed_at";
            string[] massFields =
            {
                "clean_oxygen_kg",
                "polluted_oxygen_kg",
                "clean_water_kg",
                "polluted_water_kg",
                "salt_water_kg",
                "brine_kg"
            };
            foreach (string field in massFields)
            {
                result.Quality["materials.active_world_cells." + field] = Quality(
                    status,
                    source,
                    "rolling_window_gauge",
                    "kg",
                    "active_world_visible_cells",
                    completeness,
                    aggregateCapturedAt);
            }
            result.Quality["materials.active_world_cells.valid_cell_count"] = Quality(
                status,
                source,
                "rolling_window_count",
                "count",
                "active_world_visible_cells",
                completeness,
                aggregateCapturedAt);
            string temperatureStatus = temperatureAvailable ? status : "unsupported";
            string temperatureCompleteness = temperatureAvailable ? completeness : "unknown";
            result.Quality["temperature.active_world_material_min_c"] = Quality(
                temperatureStatus,
                source,
                "rolling_window_extreme",
                "degC",
                "active_world_visible_material_cells",
                temperatureCompleteness,
                aggregateCapturedAt);
            result.Quality["temperature.active_world_material_max_c"] = Quality(
                temperatureStatus,
                source,
                "rolling_window_extreme",
                "degC",
                "active_world_visible_material_cells",
                temperatureCompleteness,
                aggregateCapturedAt);
        }

        private static void AddCellProgressQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["materials.active_world_cells.scan_complete"] = Quality(
                status,
                source,
                "rolling_scan_progress",
                "boolean",
                "active_world_visible_cells",
                completeness,
                "snapshot.ts");
            result.Quality["materials.active_world_cells.scan_progress_ratio"] = Quality(
                status,
                source,
                "rolling_scan_progress",
                "ratio",
                "active_world_visible_cells",
                completeness,
                "snapshot.ts");
        }

        private static void ReadPower(
            SurvivalTelemetryReadResult result,
            WorldContainer activeWorld)
        {
            const string source = "Components.Batteries(powerTransformer==null)+EnergyConsumers+Generators(excluding PowerTransformer)";
            if (activeWorld == null)
            {
                AddPowerQuality(result, "unsupported", source, "unknown");
                return;
            }

            try
            {
                ClusterManager manager = ClusterManager.Instance;
                int activeWorldId = activeWorld.id;
                if (!PowerWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    AddPowerQuality(result, "error", source, "unknown");
                    return;
                }

                double storedJoules = 0.0;
                double capacityJoules = 0.0;
                double activeLoadWatts = 0.0;
                double activeRatedGenerationWatts = 0.0;
                int batteryCount = 0;
                int generatorCount = 0;
                int activeGeneratorCount = 0;
                bool storedFinite = true;
                bool capacityUnbounded = false;
                bool capacityInvalid = false;
                bool activeLoadFinite = true;
                bool activeRatedGenerationFinite = true;
                List<KMonoBehaviour> boundPowerComponents = new List<KMonoBehaviour>();

                IEnumerable<Battery> batteries = Components.Batteries.GetWorldItems(activeWorldId, false);
                if (batteries == null)
                {
                    throw new InvalidOperationException("battery_registry_unavailable");
                }
                HashSet<int> batteryInstanceIds = new HashSet<int>();
                foreach (Battery battery in batteries)
                {
                    if (battery == null)
                    {
                        throw new InvalidOperationException("battery_identity_invalid");
                    }
                    if (battery.GetMyWorldId() != activeWorldId)
                    {
                        throw new InvalidOperationException("battery_world_mismatch");
                    }
                    string failureReason;
                    if (!TryRegisterPowerComponentIdentity(
                        battery.GetInstanceID(),
                        batteryInstanceIds,
                        PowerRegistryEnumerationLimit,
                        out failureReason))
                    {
                        throw new InvalidOperationException("battery_" + failureReason);
                    }
                    boundPowerComponents.Add(battery);

                    // Transformers are registered as Battery components, but
                    // their Capacity is a power rating (W), not stored energy
                    // (J).  Mixing them into this aggregate corrupts both the
                    // numerator and denominator of battery_percent.
                    // Keep the explicit null term in the scoped exclusion as a
                    // v1 producer-proof marker.  The preceding check makes its
                    // null arm unreachable: null/destroyed entries fail closed.
                    if (battery == null || battery.powerTransformer != null)
                    {
                        continue;
                    }
                    batteryCount++;
                    if (!TryAccumulateNonNegativeFinite(ref storedJoules, battery.JoulesAvailable))
                    {
                        storedFinite = false;
                    }
                    AccumulateCapacity(
                        ref capacityJoules,
                        battery.Capacity,
                        ref capacityUnbounded,
                        ref capacityInvalid);
                }
                if (!PowerWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    throw new InvalidOperationException("active_world_changed_during_battery_read");
                }

                IEnumerable<EnergyConsumer> consumers = Components.EnergyConsumers.GetWorldItems(activeWorldId, false);
                if (consumers == null)
                {
                    throw new InvalidOperationException("consumer_registry_unavailable");
                }
                HashSet<int> consumerInstanceIds = new HashSet<int>();
                foreach (EnergyConsumer consumer in consumers)
                {
                    if (consumer == null)
                    {
                        throw new InvalidOperationException("consumer_identity_invalid");
                    }
                    if (consumer.GetMyWorldId() != activeWorldId)
                    {
                        throw new InvalidOperationException("consumer_world_mismatch");
                    }
                    string failureReason;
                    if (!TryRegisterPowerComponentIdentity(
                        consumer.GetInstanceID(),
                        consumerInstanceIds,
                        PowerRegistryEnumerationLimit,
                        out failureReason))
                    {
                        throw new InvalidOperationException("consumer_" + failureReason);
                    }
                    boundPowerComponents.Add(consumer);
                    if (!TryAccumulateNonNegativeFinite(ref activeLoadWatts, consumer.WattsUsed))
                    {
                        activeLoadFinite = false;
                    }
                }
                if (!PowerWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    throw new InvalidOperationException("active_world_changed_during_consumer_read");
                }

                IEnumerable<Generator> generators = Components.Generators.GetWorldItems(activeWorldId, false);
                if (generators == null)
                {
                    throw new InvalidOperationException("generator_registry_unavailable");
                }
                HashSet<int> generatorInstanceIds = new HashSet<int>();
                foreach (Generator generator in generators)
                {
                    if (generator == null)
                    {
                        throw new InvalidOperationException("generator_identity_invalid");
                    }
                    if (generator.GetMyWorldId() != activeWorldId)
                    {
                        throw new InvalidOperationException("generator_world_mismatch");
                    }
                    string failureReason;
                    if (!TryRegisterPowerComponentIdentity(
                        generator.GetInstanceID(),
                        generatorInstanceIds,
                        PowerRegistryEnumerationLimit,
                        out failureReason))
                    {
                        throw new InvalidOperationException("generator_" + failureReason);
                    }
                    boundPowerComponents.Add(generator);

                    // PowerTransformer inherits Generator and is registered in
                    // Components.Generators, but it only transfers upstream
                    // energy to a downstream circuit.  Counting it here would
                    // manufacture generation capacity and a false independent
                    // source from the same energy a real generator supplied.
                    // The redundant null term preserves the v1 source proof;
                    // null/destroyed entries have already failed above.
                    if (generator == null || generator is PowerTransformer)
                    {
                        continue;
                    }
                    generatorCount++;
                    if (generator.IsProducingPower())
                    {
                        activeGeneratorCount++;
                        if (!TryAccumulateNonNegativeFinite(
                            ref activeRatedGenerationWatts,
                            generator.WattageRating))
                        {
                            activeRatedGenerationFinite = false;
                        }
                    }
                }
                if (!PowerWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    throw new InvalidOperationException("active_world_changed_during_generator_read");
                }
                foreach (KMonoBehaviour component in boundPowerComponents)
                {
                    if (component == null || component.GetMyWorldId() != activeWorldId)
                    {
                        throw new InvalidOperationException("power_component_world_binding_changed");
                    }
                }
                if (!PowerWorldBindingMatches(manager, activeWorld, activeWorldId))
                {
                    throw new InvalidOperationException("active_world_changed_during_component_recheck");
                }

                result.Power["active_load_watts"] = activeLoadFinite
                    ? (object)Math.Round(activeLoadWatts, 3)
                    : null;
                result.Power["active_rated_generation_watts"] = activeRatedGenerationFinite
                    ? (object)Math.Round(activeRatedGenerationWatts, 3)
                    : null;
                result.Power["battery_count"] = batteryCount;
                result.Power["consumer_count"] = consumerInstanceIds.Count;
                result.Power["generator_count"] = generatorCount;
                result.Power["active_generator_count"] = activeGeneratorCount;
                AddPowerQuality(result, "observed", source, "complete");
                string storageMode = capacityInvalid
                    ? "invalid"
                    : capacityUnbounded
                        ? "unbounded"
                        : "bounded";
                PublishBatteryTelemetry(
                    result,
                    source,
                    storedJoules,
                    storedFinite,
                    capacityJoules,
                    storageMode);
                if (!activeLoadFinite)
                {
                    AddActiveLoadQuality(result, "error", source, "unknown");
                }
                if (!activeRatedGenerationFinite)
                {
                    AddActiveRatedGenerationQuality(result, "error", source, "unknown");
                }
            }
            catch
            {
                AddPowerQuality(result, "error", source, "unknown");
            }
        }

        private static bool PowerWorldBindingMatches(
            ClusterManager manager,
            WorldContainer activeWorld,
            int activeWorldId)
        {
            return manager != null &&
                activeWorld != null &&
                ReferenceEquals(ClusterManager.Instance, manager) &&
                ReferenceEquals(manager.activeWorld, activeWorld) &&
                manager.activeWorldId == activeWorldId &&
                activeWorld.id == activeWorldId;
        }

        private static bool TryRegisterPowerComponentIdentity(
            int instanceId,
            HashSet<int> instanceIds,
            int enumerationLimit,
            out string failureReason)
        {
            if (instanceId == 0 || instanceIds == null || enumerationLimit <= 0)
            {
                failureReason = "component_identity_invalid";
                return false;
            }
            if (instanceIds.Contains(instanceId))
            {
                failureReason = "component_duplicate";
                return false;
            }
            if (instanceIds.Count >= enumerationLimit)
            {
                failureReason = "component_limit_exceeded";
                return false;
            }

            instanceIds.Add(instanceId);
            failureReason = null;
            return true;
        }

        private static void PublishBatteryTelemetry(
            SurvivalTelemetryReadResult result,
            string source,
            double storedJoules,
            bool storedFinite,
            double capacityJoules,
            string storageMode)
        {
            storedFinite = storedFinite && IsFinite(storedJoules);
            bool capacityBounded = storageMode == "bounded" && IsFinite(capacityJoules);
            bool capacityUnbounded = storageMode == "unbounded" && IsFinite(capacityJoules);
            double publishedStoredJoules = storedFinite ? Math.Round(storedJoules, 3) : 0.0;
            double publishedCapacityJoules = capacityBounded ? Math.Round(capacityJoules, 3) : 0.0;
            storedFinite = storedFinite && IsFinite(publishedStoredJoules);
            capacityBounded = capacityBounded && IsFinite(publishedCapacityJoules);
            if (!capacityBounded && !capacityUnbounded)
            {
                storageMode = "invalid";
            }

            // A bounded store cannot truthfully contain more energy than its
            // reported capacity.  Keep the independently valid bounded mode
            // and capacity, but fail the contradictory stored gauge closed;
            // the v2 contract repeats this invariant as defense in depth.
            if (storedFinite && capacityBounded &&
                (storedJoules > capacityJoules || publishedStoredJoules > publishedCapacityJoules))
            {
                storedFinite = false;
            }

            result.Power["storage_mode"] = storageMode;
            result.Power["stored_joules"] = storedFinite
                ? (object)publishedStoredJoules
                : null;
            result.Power["capacity_joules"] = capacityBounded
                ? (object)publishedCapacityJoules
                : null;

            double batteryPercent = 0.0;
            bool batteryPercentFinite = false;
            if (storedFinite && capacityBounded && publishedCapacityJoules > 0.0)
            {
                batteryPercent = publishedStoredJoules / publishedCapacityJoules * 100.0;
                batteryPercentFinite = IsFinite(batteryPercent);
            }
            result.Power["battery_percent"] = batteryPercentFinite
                ? (object)Math.Round(batteryPercent, 3)
                : null;

            AddStorageModeQuality(
                result,
                storageMode == "invalid" ? "error" : "observed",
                source,
                storageMode == "invalid" ? "unknown" : "complete");
            AddBatteryGaugeQuality(
                result,
                "stored_joules",
                storedFinite ? "observed" : "error",
                source,
                storedFinite ? "complete" : "unknown");
            AddBatteryGaugeQuality(
                result,
                "capacity_joules",
                capacityBounded
                    ? "observed"
                    : capacityUnbounded
                        ? "unsupported"
                        : "error",
                source,
                capacityBounded || capacityUnbounded ? "complete" : "unknown");

            if (capacityUnbounded)
            {
                AddBatteryPercentQuality(result, "unsupported", source, "complete");
            }
            else if (!capacityBounded || !storedFinite ||
                (publishedCapacityJoules > 0.0 && !batteryPercentFinite))
            {
                AddBatteryPercentQuality(result, "error", source, "unknown");
            }
            else if (publishedCapacityJoules <= 0.0)
            {
                AddBatteryPercentQuality(result, "unsupported", source, "complete");
            }
            else
            {
                AddBatteryPercentQuality(result, "observed", source, "complete");
            }
        }

        private static void AccumulateCapacity(
            ref double total,
            double rawCapacity,
            ref bool unbounded,
            ref bool invalid)
        {
            // Positive infinity is the one recognized unbounded-capacity
            // signal.  It is represented by storage_mode=unbounded plus null
            // capacity/percent; it must never cross the JSON boundary itself.
            if (double.IsPositiveInfinity(rawCapacity))
            {
                unbounded = true;
                return;
            }

            // NaN, negative infinity, and overflow while accumulating finite
            // members are invalid.  Keep scanning/counting the full battery
            // set, but never publish the partial capacity total.
            if (!TryAccumulateNonNegativeFinite(ref total, rawCapacity))
            {
                invalid = true;
            }
        }

        private static bool TryAccumulateNonNegativeFinite(ref double total, double value)
        {
            if (!IsFinite(value) || value < 0.0)
            {
                return false;
            }

            double next = total + value;
            if (!IsFinite(next))
            {
                return false;
            }

            total = next;
            return true;
        }

        private static bool IsFinite(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }

        private static void AddPowerQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            AddStorageModeQuality(result, status, source, completeness);
            AddBatteryGaugeQuality(
                result,
                "stored_joules",
                status,
                source,
                completeness);
            AddBatteryGaugeQuality(
                result,
                "capacity_joules",
                status,
                source,
                completeness);
            AddBatteryPercentQuality(result, status, source, completeness);
            AddActiveLoadQuality(result, status, source, completeness);
            AddActiveRatedGenerationQuality(result, status, source, completeness);
            AddPowerCountQuality(result, status, source, completeness);
        }

        private static void AddStorageModeQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power.storage_mode"] = Quality(
                status,
                source,
                "storage_capacity_mode",
                "enum",
                "active_world_registered_batteries",
                completeness);
        }

        private static void AddBatteryGaugeQuality(
            SurvivalTelemetryReadResult result,
            string field,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power." + field] = Quality(
                status,
                source,
                "raw_gauge",
                "J",
                "active_world_registered_batteries",
                completeness);
        }

        private static void AddBatteryPercentQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power.battery_percent"] = Quality(
                status,
                source,
                "derived_ratio",
                "percent",
                "active_world_registered_batteries",
                completeness);
        }

        private static void AddActiveLoadQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power.active_load_watts"] = Quality(
                status,
                source,
                "instantaneous_load",
                "W",
                "active_world_registered_power_components",
                completeness);
        }

        private static void AddActiveRatedGenerationQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power.active_rated_generation_watts"] = Quality(
                status,
                source,
                "active_non_transformer_rated_capacity",
                "W",
                "active_world_registered_non_transformer_generators",
                completeness);
        }

        private static void AddPowerCountQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            result.Quality["power.battery_count"] = Quality(
                status,
                source,
                "raw_count",
                "count",
                "active_world_registered_batteries",
                completeness);
            result.Quality["power.consumer_count"] = Quality(
                status,
                source,
                "raw_count",
                "count",
                "active_world_registered_power_components",
                completeness);
            foreach (string field in new[] { "generator_count", "active_generator_count" })
            {
                result.Quality["power." + field] = Quality(
                    status,
                    source,
                    "raw_non_transformer_generator_count",
                    "count",
                    "active_world_registered_non_transformer_generators",
                    completeness);
            }
        }

        private static void ReadRockets(SurvivalTelemetryReadResult result)
        {
            const string dlcSource = "DlcManager.FeatureClusterSpaceEnabled+Components.Clustercrafts";
            const string classicSource = "SpacecraftManager.GetSpacecraft";
            try
            {
                if (DlcManager.FeatureClusterSpaceEnabled())
                {
                    int grounded = 0;
                    int launching = 0;
                    int inFlight = 0;
                    int landing = 0;
                    foreach (Clustercraft craft in Components.Clustercrafts.Items)
                    {
                        if (craft == null)
                        {
                            continue;
                        }
                        switch (craft.Status)
                        {
                            case Clustercraft.CraftStatus.Grounded:
                                grounded++;
                                break;
                            case Clustercraft.CraftStatus.Launching:
                                launching++;
                                break;
                            case Clustercraft.CraftStatus.InFlight:
                                inFlight++;
                                break;
                            case Clustercraft.CraftStatus.Landing:
                                landing++;
                                break;
                        }
                    }
                    int total = grounded + launching + inFlight + landing;
                    result.Rockets["total"] = total;
                    result.Rockets["grounded"] = grounded;
                    result.Rockets["launching"] = launching;
                    result.Rockets["in_flight"] = inFlight;
                    result.Rockets["landing"] = landing;
                    AddRocketQuality(result, "observed", dlcSource, "complete");
                    return;
                }

                IList<Spacecraft> spacecraft = SpacecraftManager.instance?.GetSpacecraft();
                if (spacecraft == null)
                {
                    AddRocketQuality(result, "unsupported", classicSource, "unknown");
                    return;
                }

                result.Rockets["total"] = spacecraft.Count;
                result.Rockets["grounded"] = null;
                result.Rockets["launching"] = null;
                result.Rockets["in_flight"] = null;
                result.Rockets["landing"] = null;
                AddRocketQuality(result, "observed", classicSource, "complete");
                foreach (string field in new[] { "grounded", "launching", "in_flight", "landing" })
                {
                    result.Quality["rockets." + field] = Quality(
                        "unsupported",
                        classicSource,
                        "raw_count",
                        "count",
                        "cluster",
                        "unknown");
                }
            }
            catch
            {
                AddRocketQuality(result, "error", "rocket_registry", "unknown");
            }
        }

        private static void AddRocketQuality(
            SurvivalTelemetryReadResult result,
            string status,
            string source,
            string completeness)
        {
            foreach (string field in new[] { "total", "grounded", "launching", "in_flight", "landing" })
            {
                result.Quality["rockets." + field] = Quality(
                    status,
                    source,
                    "raw_count",
                    "count",
                    "cluster",
                    completeness);
            }
        }

        private static void SeedUnsupportedDecisionFields(SurvivalTelemetryReadResult result)
        {
            Dictionary<string, string> production = new Dictionary<string, string>
            {
                { "food_kcal_per_cycle", "kcal/cycle" },
                { "oxygen_kg_per_cycle", "kg/cycle" },
                { "water_kg_per_cycle", "kg/cycle" },
                { "power_watts", "W" }
            };
            foreach (KeyValuePair<string, string> field in production)
            {
                result.Production[field.Key] = null;
                result.Quality["production." + field.Key] = Unsupported(
                    "No gap-free production counter window is available.",
                    "window_rate",
                    field.Value,
                    "all_populated_worlds");
            }

            Dictionary<string, string> consumption = new Dictionary<string, string>
            {
                { "oxygen_kg_per_cycle", "kg/cycle" },
                { "water_kg_per_cycle", "kg/cycle" },
                { "power_watts", "W" }
            };
            foreach (KeyValuePair<string, string> field in consumption)
            {
                result.Consumption[field.Key] = null;
                result.Quality["consumption." + field.Key] = Unsupported(
                    "No gap-free consumption counter window is available.",
                    "window_rate",
                    field.Value,
                    "all_populated_worlds");
            }

            result.Power["battery_percent"] = null;
            result.Power["critical_load_covered"] = null;
            result.Power["independent_source_count"] = null;
            result.Oxygen["independent_source_count"] = null;
            result.Water["independent_source_count"] = null;
            result.Temperature["critical_zones_safe"] = null;
            result.Temperature["min_c"] = null;
            result.Temperature["max_c"] = null;
            result.Space["mission_ready"] = null;
            result.Space["mission_completed"] = null;
            result.Space["return_ready"] = null;
            result.Space["base_reserve_safe"] = null;
            result.Resilience["n_minus_one_safe"] = null;
            result.Colony["sustainable"] = null;

            result.Quality["power.critical_load_covered"] = Unsupported("Critical consumer set and circuit topology are undefined.", "certified_assertion", "boolean", "all_populated_worlds");
            result.Quality["power.independent_source_count"] = Unsupported("Shared upstream power dependencies are not modelled.", "certified_topology_count", "count", "all_populated_worlds");
            result.Quality["oxygen.independent_source_count"] = Unsupported("Shared upstream oxygen dependencies are not modelled.", "certified_topology_count", "count", "all_populated_worlds");
            result.Quality["water.independent_source_count"] = Unsupported("Shared upstream water dependencies are not modelled.", "certified_topology_count", "count", "all_populated_worlds");
            result.Quality["temperature.critical_zones_safe"] = Unsupported("Critical zones and policy bands are not defined in raw telemetry.", "certified_assertion", "boolean", "all_populated_worlds");
            result.Quality["temperature.min_c"] = Unsupported("A policy-relevant critical-zone aggregate is not available.", "critical_zone_extreme", "degC", "all_populated_worlds_critical_zones");
            result.Quality["temperature.max_c"] = Unsupported("A policy-relevant critical-zone aggregate is not available.", "critical_zone_extreme", "degC", "all_populated_worlds_critical_zones");
            result.Quality["space.mission_ready"] = Unsupported("Rocket readiness is not equivalent to a safe mission policy.", "certified_assertion", "boolean", "cluster");
            result.Quality["space.mission_completed"] = Unsupported("No persistent official mission event ledger is available.", "raw_event", "boolean", "cluster");
            result.Quality["space.return_ready"] = Unsupported("Return path, fuel, crew life support, and landing are not jointly proven.", "certified_assertion", "boolean", "cluster");
            result.Quality["space.base_reserve_safe"] = Unsupported("Base reserve safety is a cross-domain policy conclusion.", "certified_assertion", "boolean", "save");
            result.Quality["resilience.n_minus_one_safe"] = Unsupported("No dependency graph and failure-set analysis are available.", "certified_assertion", "boolean", "all_populated_worlds");
            result.Quality["colony.sustainable"] = Unsupported("Sustainability is a multi-window certification, not a probe gauge.", "certified_assertion", "boolean", "save");
        }

        private static Dictionary<string, object> Unsupported(
            string reason,
            string semantic,
            string unit,
            string scope)
        {
            Dictionary<string, object> quality = Quality(
                "unsupported",
                "none",
                semantic,
                unit,
                scope,
                "unknown");
            quality["reason"] = reason;
            return quality;
        }

        private static Dictionary<string, object> Quality(
            string status,
            string source,
            string semantic,
            string unit,
            string scope,
            string completeness,
            string capturedAt = "snapshot.ts")
        {
            return new Dictionary<string, object>
            {
                { "status", status },
                { "source", source },
                { "semantic", semantic },
                { "unit", unit },
                { "scope", scope },
                { "completeness", completeness },
                { "captured_at", capturedAt }
            };
        }

        private sealed class ActiveWorldCellScanState
        {
            internal ActiveWorldCellScanState(
                long generation,
                int worldId,
                int offsetX,
                int offsetY,
                int width,
                int height,
                int candidateCellCount)
            {
                Generation = generation;
                WorldId = worldId;
                OffsetX = offsetX;
                OffsetY = offsetY;
                Width = width;
                Height = height;
                CandidateCellCount = candidateCellCount;
                StartedAt = DateTimeOffset.UtcNow;
                MinimumKelvin = double.PositiveInfinity;
                MaximumKelvin = double.NegativeInfinity;
            }

            internal long Generation { get; }
            internal int WorldId { get; }
            internal int OffsetX { get; }
            internal int OffsetY { get; }
            internal int Width { get; }
            internal int Height { get; }
            internal int CandidateCellCount { get; }
            internal DateTimeOffset StartedAt { get; }
            internal DateTimeOffset? CompletedAt { get; set; }
            internal int NextCandidateIndex { get; set; }
            internal int ScannedValidCellCount { get; set; }
            internal double CleanOxygen { get; set; }
            internal double PollutedOxygen { get; set; }
            internal double CleanWater { get; set; }
            internal double PollutedWater { get; set; }
            internal double SaltWater { get; set; }
            internal double Brine { get; set; }
            internal double MinimumKelvin { get; set; }
            internal double MaximumKelvin { get; set; }
            internal bool Complete { get; set; }

            internal bool Matches(int worldId, int offsetX, int offsetY, int width, int height)
            {
                return WorldId == worldId &&
                    OffsetX == offsetX &&
                    OffsetY == offsetY &&
                    Width == width &&
                    Height == height;
            }
        }

        private static double RoundMass(double value)
        {
            return Math.Round(value, 6);
        }
    }

    internal sealed class SurvivalTelemetryReadResult
    {
        internal Dictionary<string, object> ObservationScope { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Materials { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Production { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Consumption { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Power { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Oxygen { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Water { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Temperature { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Rockets { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Space { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Resilience { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Colony { get; } = new Dictionary<string, object>();
        internal Dictionary<string, object> Quality { get; } = new Dictionary<string, object>();
    }
}
