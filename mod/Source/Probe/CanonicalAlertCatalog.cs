using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;

namespace OniAiProbeMod.Probe
{
    internal sealed class CanonicalAlertReadResult
    {
        private CanonicalAlertReadResult(
            List<string> alerts,
            string status,
            string source,
            string failureReason)
        {
            Alerts = alerts;
            Status = status;
            Source = source;
            FailureReason = failureReason;
        }

        internal List<string> Alerts { get; }

        internal string Status { get; }

        internal string Source { get; }

        internal string FailureReason { get; }

        internal string Completeness => Status == "observed" ? "complete" : "unknown";

        internal object Value => Status == "observed" ? (object)Alerts : null;

        internal static CanonicalAlertReadResult Observed(List<string> alerts, string source)
        {
            return new CanonicalAlertReadResult(alerts, "observed", source, null);
        }

        internal static CanonicalAlertReadResult Unsupported(string source, string failureReason)
        {
            return new CanonicalAlertReadResult(null, "unsupported", source, failureReason);
        }

        internal static CanonicalAlertReadResult Error(string source, string failureReason)
        {
            return new CanonicalAlertReadResult(null, "error", source, failureReason);
        }
    }

    /// <summary>
    /// Versioned vocabulary and canonicalization boundary for active-world alerts.
    /// </summary>
    internal static class CanonicalAlertCatalog
    {
        internal const string CatalogId = "oni.alert.catalog.active_world.v1";
        internal const int MaximumAlertCount = 128;

        internal const string BionicLowBattery = "bionic_low_battery";
        internal const string BionicOffline = "bionic_offline";
        internal const string Exhaustion = "exhaustion";
        internal const string HighStress = "high_stress";
        internal const string Hyperthermia = "hyperthermia";
        internal const string Hypothermia = "hypothermia";
        internal const string Incapacitated = "incapacitated";
        internal const string LowOxygen = "low_oxygen";
        internal const string MajorSickness = "major_sickness";
        internal const string Sickness = "sickness";
        internal const string Starvation = "starvation";
        internal const string Suffocation = "suffocation";
        internal const string UrgentToilet = "urgent_toilet";
        internal const string Wounded = "wounded";

        private static readonly Regex AlertIdPattern = new Regex(
            @"^[a-z][a-z0-9_]{0,63}$",
            RegexOptions.CultureInvariant);

        private static readonly HashSet<string> KnownIds = new HashSet<string>(
            new[]
            {
                BionicLowBattery,
                BionicOffline,
                Exhaustion,
                HighStress,
                Hyperthermia,
                Hypothermia,
                Incapacitated,
                LowOxygen,
                MajorSickness,
                Sickness,
                Starvation,
                Suffocation,
                UrgentToilet,
                Wounded
            },
            StringComparer.Ordinal);

        internal static IReadOnlyCollection<string> AllKnownIds => KnownIds;

        internal static bool IsKnown(string alertId)
        {
            return !string.IsNullOrEmpty(alertId) && KnownIds.Contains(alertId);
        }

        internal static CanonicalAlertReadResult Observed(IEnumerable<string> candidates, string source)
        {
            if (candidates == null)
            {
                return CanonicalAlertReadResult.Error(source, "alert_input_missing");
            }

            SortedSet<string> normalized = new SortedSet<string>(StringComparer.Ordinal);
            foreach (string candidate in candidates)
            {
                if (string.IsNullOrEmpty(candidate) || !AlertIdPattern.IsMatch(candidate))
                {
                    return CanonicalAlertReadResult.Error(source, "alert_id_invalid");
                }
                normalized.Add(candidate);
                if (normalized.Count > MaximumAlertCount)
                {
                    return CanonicalAlertReadResult.Error(source, "alert_limit_exceeded");
                }
            }

            foreach (string candidate in normalized)
            {
                if (!KnownIds.Contains(candidate))
                {
                    return CanonicalAlertReadResult.Error(source, "unknown_alert_id");
                }
            }

            return CanonicalAlertReadResult.Observed(new List<string>(normalized), source);
        }
    }
}
