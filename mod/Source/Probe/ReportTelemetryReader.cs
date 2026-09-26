using System;
using System.Collections.Generic;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads the game's save-global, report-instrumented counters without
    /// creating report entries or deriving decision-grade rates in the Probe.
    /// </summary>
    internal static class ReportTelemetryReader
    {
        private const int ReportEntryEnumerationLimit = 256;
        private const string CurrentPeriodKey = "current";
        private const string PreviousPeriodKey = "previous_closed";
        private const string CurrentSource = "ReportManager.TodaysReport";
        private const string PreviousSource = "ReportManager.reports[last]";
        private const string CounterScope = "save_report_manager_oxygen_created";
        private const string EnergyCounterScope = "save_report_manager_energy_created";
        private const string CounterCompleteness = "complete_for_instrumented_report_events";

        internal static ReportTelemetryReadResult Read()
        {
            ReportTelemetryReadResult result = new ReportTelemetryReadResult();
            SetPeriodQuality(
                result,
                CurrentPeriodKey,
                CurrentSource,
                "unsupported",
                "unknown",
                "ReportManager is not ready.");
            SetPeriodQuality(
                result,
                PreviousPeriodKey,
                PreviousSource,
                "unsupported",
                "unknown",
                "No closed report is available.");

            ReportManager manager;
            try
            {
                manager = ReportManager.Instance;
            }
            catch (Exception ex)
            {
                SetPeriodQuality(result, CurrentPeriodKey, CurrentSource, "error", "unknown", ex.GetType().Name);
                SetPeriodQuality(result, PreviousPeriodKey, PreviousSource, "error", "unknown", ex.GetType().Name);
                return result;
            }

            // Managed null check: ReportManager clears Instance in OnCleanUp, and
            // the counters read below are plain managed fields.
            if (ReferenceEquals(manager, null))
            {
                return result;
            }

            ReportCounterSnapshot current = null;
            try
            {
                current = ReadPeriod(
                    result,
                    CurrentPeriodKey,
                    CurrentSource,
                    manager.TodaysReport);
            }
            catch (Exception ex)
            {
                ClearPeriod(result, CurrentPeriodKey);
                SetPeriodQuality(result, CurrentPeriodKey, CurrentSource, "error", "unknown", ex.GetType().Name);
            }

            try
            {
                List<ReportManager.DailyReport> closedReports = manager.reports;
                if (closedReports == null || closedReports.Count == 0)
                {
                    return result;
                }

                ReportCounterSnapshot previous = ReadPeriod(
                    result,
                    PreviousPeriodKey,
                    PreviousSource,
                    closedReports[closedReports.Count - 1]);
                if (current != null && previous != null && previous.ReportDay >= current.ReportDay)
                {
                    ClearPeriod(result, PreviousPeriodKey);
                    SetPeriodQuality(
                        result,
                        PreviousPeriodKey,
                        PreviousSource,
                        "error",
                        "unknown",
                        "The last closed report epoch is not older than the open report epoch.");
                }
            }
            catch (Exception ex)
            {
                ClearPeriod(result, PreviousPeriodKey);
                SetPeriodQuality(result, PreviousPeriodKey, PreviousSource, "error", "unknown", ex.GetType().Name);
            }

            return result;
        }

        private static ReportCounterSnapshot ReadPeriod(
            ReportTelemetryReadResult result,
            string periodKey,
            string source,
            ReportManager.DailyReport report)
        {
            try
            {
                if (!TryReadOxygenCreated(report, out ReportCounterSnapshot snapshot, out string failure))
                {
                    ClearPeriod(result, periodKey);
                    SetPeriodQuality(result, periodKey, source, "error", "unknown", failure);
                    return null;
                }

                SetPeriod(result, periodKey, snapshot);
                SetPeriodQuality(
                    result,
                    periodKey,
                    source,
                    "observed",
                    CounterCompleteness,
                    null);
                // Energy is additive: its failure never withdraws the oxygen counters.
                if (TryReadEnergyCreated(
                        report,
                        out float positiveJoules,
                        out float negativeJoules,
                        out string energyFailure))
                {
                    SetEnergy(result, periodKey, positiveJoules, negativeJoules);
                    SetEnergyQuality(result, periodKey, source, "observed", CounterCompleteness, null);
                }
                else
                {
                    ClearEnergy(result, periodKey);
                    SetEnergyQuality(result, periodKey, source, "error", "unknown", energyFailure);
                }
                return snapshot;
            }
            catch (Exception ex)
            {
                ClearPeriod(result, periodKey);
                SetPeriodQuality(result, periodKey, source, "error", "unknown", ex.GetType().Name);
                return null;
            }
        }

        private static bool TryReadOxygenCreated(
            ReportManager.DailyReport report,
            out ReportCounterSnapshot snapshot,
            out string failure)
        {
            snapshot = null;
            failure = null;
            if (report == null)
            {
                failure = "The report period is missing.";
                return false;
            }
            if (report.day < 1)
            {
                failure = "The report epoch is invalid.";
                return false;
            }
            if (report.reportEntries == null)
            {
                failure = "The report entry collection is missing.";
                return false;
            }
            if (report.reportEntries.Count > ReportEntryEnumerationLimit)
            {
                failure = "The report entry collection exceeds the bounded read limit.";
                return false;
            }

            ReportManager.ReportEntry oxygenCreated = null;
            int matches = 0;
            foreach (ReportManager.ReportEntry entry in report.reportEntries)
            {
                if (entry == null || entry.reportType != ReportManager.ReportType.OxygenCreated)
                {
                    continue;
                }

                matches++;
                oxygenCreated = entry;
            }
            if (matches != 1 || oxygenCreated == null)
            {
                failure = "The report must contain exactly one OxygenCreated entry.";
                return false;
            }

            float positive = oxygenCreated.Positive;
            float negative = oxygenCreated.Negative;
            if (float.IsNaN(positive) || float.IsInfinity(positive) ||
                float.IsNaN(negative) || float.IsInfinity(negative))
            {
                failure = "The OxygenCreated counters are not finite.";
                return false;
            }
            if (positive != oxygenCreated.Positive || negative != oxygenCreated.Negative)
            {
                failure = "The OxygenCreated counters changed during the read.";
                return false;
            }
            if (positive < 0f || negative > 0f)
            {
                failure = "The OxygenCreated counter signs are invalid.";
                return false;
            }

            snapshot = new ReportCounterSnapshot(report.day, positive, negative);
            return true;
        }

        // EnergyCreated: Generator reports the joules it produced (positive) and
        // CircuitManager the joules consumed (negative) into the same entry.
        // Every DailyReport creates one entry per report group, EnergyCreated included.
        private static bool TryReadEnergyCreated(
            ReportManager.DailyReport report,
            out float positiveJoules,
            out float negativeJoules,
            out string failure)
        {
            positiveJoules = 0f;
            negativeJoules = 0f;
            failure = null;
            ReportManager.ReportEntry energyCreated = null;
            int matches = 0;
            foreach (ReportManager.ReportEntry entry in report.reportEntries)
            {
                if (entry == null || entry.reportType != ReportManager.ReportType.EnergyCreated)
                {
                    continue;
                }

                matches++;
                energyCreated = entry;
            }
            if (matches != 1 || energyCreated == null)
            {
                failure = "The report must contain exactly one EnergyCreated entry.";
                return false;
            }

            float positive = energyCreated.Positive;
            float negative = energyCreated.Negative;
            if (float.IsNaN(positive) || float.IsInfinity(positive) ||
                float.IsNaN(negative) || float.IsInfinity(negative))
            {
                failure = "The EnergyCreated counters are not finite.";
                return false;
            }
            if (positive != energyCreated.Positive || negative != energyCreated.Negative)
            {
                failure = "The EnergyCreated counters changed during the read.";
                return false;
            }
            if (positive < 0f || negative > 0f)
            {
                failure = "The EnergyCreated counter signs are invalid.";
                return false;
            }

            positiveJoules = positive;
            negativeJoules = negative;
            return true;
        }

        private static void SetEnergy(
            ReportTelemetryReadResult result,
            string periodKey,
            float positiveJoules,
            float negativeJoules)
        {
            Dictionary<string, object> period = (Dictionary<string, object>)result.Counters[periodKey];
            Dictionary<string, object> energyCreated =
                (Dictionary<string, object>)period["energy_created"];
            energyCreated["positive_joules"] = positiveJoules;
            energyCreated["negative_joules"] = negativeJoules;
        }

        private static void ClearEnergy(ReportTelemetryReadResult result, string periodKey)
        {
            Dictionary<string, object> period = (Dictionary<string, object>)result.Counters[periodKey];
            Dictionary<string, object> energyCreated =
                (Dictionary<string, object>)period["energy_created"];
            energyCreated["positive_joules"] = null;
            energyCreated["negative_joules"] = null;
        }

        private static void SetEnergyQuality(
            ReportTelemetryReadResult result,
            string periodKey,
            string source,
            string status,
            string completeness,
            string reason)
        {
            string prefix = "report_counters." + periodKey;
            result.Quality[prefix + ".energy_created.positive_joules"] = Quality(
                status,
                source + ".reportEntries[EnergyCreated].Positive",
                periodKey == CurrentPeriodKey
                    ? "raw_open_report_positive_counter"
                    : "raw_closed_report_positive_counter",
                "J",
                EnergyCounterScope,
                completeness,
                reason);
            result.Quality[prefix + ".energy_created.negative_joules"] = Quality(
                status,
                source + ".reportEntries[EnergyCreated].Negative",
                periodKey == CurrentPeriodKey
                    ? "raw_open_report_negative_counter"
                    : "raw_closed_report_negative_counter",
                "J",
                EnergyCounterScope,
                completeness,
                reason);
        }

        private static void SetPeriod(
            ReportTelemetryReadResult result,
            string periodKey,
            ReportCounterSnapshot snapshot)
        {
            Dictionary<string, object> period = (Dictionary<string, object>)result.Counters[periodKey];
            Dictionary<string, object> oxygenCreated =
                (Dictionary<string, object>)period["oxygen_created"];
            period["report_day"] = snapshot.ReportDay;
            oxygenCreated["positive_kg"] = snapshot.PositiveKg;
            oxygenCreated["negative_kg"] = snapshot.NegativeKg;
        }

        private static void ClearPeriod(ReportTelemetryReadResult result, string periodKey)
        {
            Dictionary<string, object> period = (Dictionary<string, object>)result.Counters[periodKey];
            Dictionary<string, object> oxygenCreated =
                (Dictionary<string, object>)period["oxygen_created"];
            period["report_day"] = null;
            oxygenCreated["positive_kg"] = null;
            oxygenCreated["negative_kg"] = null;
            ClearEnergy(result, periodKey);
        }

        private static void SetPeriodQuality(
            ReportTelemetryReadResult result,
            string periodKey,
            string source,
            string status,
            string completeness,
            string reason)
        {
            string prefix = "report_counters." + periodKey;
            result.Quality[prefix + ".report_day"] = Quality(
                status,
                source + ".day",
                periodKey == CurrentPeriodKey ? "raw_open_report_epoch" : "raw_closed_report_epoch",
                "report_day",
                "save",
                status == "observed" ? "complete" : completeness,
                reason);
            result.Quality[prefix + ".oxygen_created.positive_kg"] = Quality(
                status,
                source + ".reportEntries[OxygenCreated].Positive",
                periodKey == CurrentPeriodKey
                    ? "raw_open_report_positive_counter"
                    : "raw_closed_report_positive_counter",
                "kg",
                CounterScope,
                completeness,
                reason);
            result.Quality[prefix + ".oxygen_created.negative_kg"] = Quality(
                status,
                source + ".reportEntries[OxygenCreated].Negative",
                periodKey == CurrentPeriodKey
                    ? "raw_open_report_negative_counter"
                    : "raw_closed_report_negative_counter",
                "kg",
                CounterScope,
                completeness,
                reason);
            // The period state (unsupported/error) covers energy too; a
            // successful period read replaces it with the energy read's own.
            SetEnergyQuality(result, periodKey, source, status, completeness, reason);
        }

        private static Dictionary<string, object> Quality(
            string status,
            string source,
            string semantic,
            string unit,
            string scope,
            string completeness,
            string reason)
        {
            Dictionary<string, object> quality = new Dictionary<string, object>
            {
                { "status", status },
                { "source", source },
                { "semantic", semantic },
                { "unit", unit },
                { "scope", scope },
                { "completeness", completeness },
                { "captured_at", "snapshot.ts" }
            };
            if (!string.IsNullOrEmpty(reason))
            {
                quality["reason"] = reason;
            }
            return quality;
        }

        private sealed class ReportCounterSnapshot
        {
            internal ReportCounterSnapshot(int reportDay, float positiveKg, float negativeKg)
            {
                ReportDay = reportDay;
                PositiveKg = positiveKg;
                NegativeKg = negativeKg;
            }

            internal int ReportDay { get; }

            internal float PositiveKg { get; }

            internal float NegativeKg { get; }
        }
    }

    internal sealed class ReportTelemetryReadResult
    {
        internal ReportTelemetryReadResult()
        {
            Counters["current"] = EmptyPeriod();
            Counters["previous_closed"] = EmptyPeriod();
        }

        internal Dictionary<string, object> Counters { get; } =
            new Dictionary<string, object>(StringComparer.Ordinal);

        internal Dictionary<string, object> Quality { get; } =
            new Dictionary<string, object>(StringComparer.Ordinal);

        private static Dictionary<string, object> EmptyPeriod()
        {
            return new Dictionary<string, object>
            {
                { "report_day", null },
                {
                    "oxygen_created",
                    new Dictionary<string, object>
                    {
                        { "positive_kg", null },
                        { "negative_kg", null }
                    }
                },
                {
                    "energy_created",
                    new Dictionary<string, object>
                    {
                        { "positive_joules", null },
                        { "negative_joules", null }
                    }
                }
            };
        }
    }
}
