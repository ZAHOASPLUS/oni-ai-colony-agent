using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;

namespace ReportTelemetryHarness
{
    // Drives ReportTelemetryReader against the real ReportManager, DailyReport
    // and ReportEntry types (allocated without Unity).  The EnergyCreated
    // counters (joules) are published beside OxygenCreated in the same layout,
    // and an energy failure never withdraws the oxygen counters.
    internal static class Program
    {
        private const BindingFlags AnyInstance = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            try
            {
                Run();
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.report-telemetry-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(Passed.ToArray())
                }.ToString(Newtonsoft.Json.Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
        }

        private static void Run()
        {
            // Both periods observed: energy mirrors oxygen for current and previous_closed.
            InstallManager(
                Report(8, Entry(ReportManager.ReportType.OxygenCreated, 90f, -60f), Entry(ReportManager.ReportType.EnergyCreated, 120000f, -48000f)),
                Report(7, Entry(ReportManager.ReportType.OxygenCreated, 300f, -250f), Entry(ReportManager.ReportType.EnergyCreated, 360000f, -300000f)));
            ReportTelemetryReadResult result = ReportTelemetryReader.Read();
            foreach (string period in new[] { "current", "previous_closed" })
            {
                bool closed = period == "previous_closed";
                string source = closed ? "ReportManager.reports[last]" : "ReportManager.TodaysReport";
                Dictionary<string, object> energy = Counter(result, period, "energy_created");
                Require(Equal(energy["positive_joules"], closed ? 360000f : 120000f) &&
                    Equal(energy["negative_joules"], closed ? -300000f : -48000f),
                    period + " energy counters are wrong");
                Require(energy.Count == 2, period + " energy_created has unexpected fields");
                foreach (string sign in new[] { "positive", "negative" })
                {
                    Dictionary<string, object> quality = QualityOf(result, "report_counters." + period + ".energy_created." + sign + "_joules");
                    Require((string)quality["status"] == "observed" &&
                        (string)quality["unit"] == "J" &&
                        (string)quality["scope"] == "save_report_manager_energy_created" &&
                        (string)quality["completeness"] == "complete_for_instrumented_report_events" &&
                        (string)quality["source"] == source + ".reportEntries[EnergyCreated]." + (sign == "positive" ? "Positive" : "Negative") &&
                        (string)quality["semantic"] == "raw_" + (closed ? "closed" : "open") + "_report_" + sign + "_counter" &&
                        (string)quality["captured_at"] == "snapshot.ts",
                        period + " " + sign + " energy quality is wrong");
                }
                Dictionary<string, object> oxygen = Counter(result, period, "oxygen_created");
                Require(Equal(oxygen["positive_kg"], closed ? 300f : 90f), period + " oxygen counters changed");
            }
            Passed.Add("energy_mirrors_oxygen");

            // A bad energy entry is an energy error only; oxygen stays observed.
            InstallManager(
                Report(8, Entry(ReportManager.ReportType.OxygenCreated, 90f, -60f), Entry(ReportManager.ReportType.EnergyCreated, -1f, 0f)),
                Report(7, Entry(ReportManager.ReportType.OxygenCreated, 300f, -250f)));
            result = ReportTelemetryReader.Read();
            Require(Counter(result, "current", "energy_created")["positive_joules"] == null &&
                (string)QualityOf(result, "report_counters.current.energy_created.positive_joules")["status"] == "error",
                "invalid energy signs were published");
            Require(Counter(result, "previous_closed", "energy_created")["negative_joules"] == null &&
                ((string)QualityOf(result, "report_counters.previous_closed.energy_created.negative_joules")["reason"]).Contains("EnergyCreated"),
                "a missing EnergyCreated entry was not reported");
            Require(Equal(Counter(result, "current", "oxygen_created")["positive_kg"], 90f) &&
                (string)QualityOf(result, "report_counters.current.oxygen_created.positive_kg")["status"] == "observed" &&
                (string)QualityOf(result, "report_counters.previous_closed.oxygen_created.positive_kg")["status"] == "observed",
                "an energy failure withdrew the oxygen counters");
            Passed.Add("energy_failure_is_isolated");

            // A period failure (epoch order) clears energy with oxygen.
            InstallManager(
                Report(7, Entry(ReportManager.ReportType.OxygenCreated, 1f, 0f), Entry(ReportManager.ReportType.EnergyCreated, 1f, 0f)),
                Report(7, Entry(ReportManager.ReportType.OxygenCreated, 1f, 0f), Entry(ReportManager.ReportType.EnergyCreated, 1f, 0f)));
            result = ReportTelemetryReader.Read();
            Require(Counter(result, "previous_closed", "energy_created")["positive_joules"] == null &&
                (string)QualityOf(result, "report_counters.previous_closed.energy_created.positive_joules")["status"] == "error" &&
                Counter(result, "previous_closed", "oxygen_created")["positive_kg"] == null,
                "a rejected closed period kept its energy counters");
            Passed.Add("period_failure_clears_energy");

            // No ReportManager: both counters are unsupported and null.
            typeof(ReportManager).GetProperty("Instance", AnyStatic).GetSetMethod(true).Invoke(null, new object[] { null });
            result = ReportTelemetryReader.Read();
            Require(Counter(result, "current", "energy_created")["positive_joules"] == null &&
                (string)QualityOf(result, "report_counters.current.energy_created.positive_joules")["status"] == "unsupported",
                "a missing ReportManager did not publish unsupported energy");
            Passed.Add("unavailable_is_unsupported");
        }

        private static void InstallManager(ReportManager.DailyReport today, ReportManager.DailyReport lastClosed)
        {
            ReportManager manager = (ReportManager)RuntimeHelpers.GetUninitializedObject(typeof(ReportManager));
            typeof(ReportManager).GetField("todaysReport", AnyInstance).SetValue(manager, today);
            typeof(ReportManager).GetField("dailyReports", AnyInstance)
                .SetValue(manager, new List<ReportManager.DailyReport> { lastClosed });
            typeof(ReportManager).GetProperty("Instance", AnyStatic).GetSetMethod(true).Invoke(null, new object[] { manager });
        }

        private static ReportManager.DailyReport Report(int day, params ReportManager.ReportEntry[] entries)
        {
            ReportManager.DailyReport report =
                (ReportManager.DailyReport)RuntimeHelpers.GetUninitializedObject(typeof(ReportManager.DailyReport));
            report.day = day;
            report.reportEntries = entries.ToList();
            return report;
        }

        private static ReportManager.ReportEntry Entry(ReportManager.ReportType type, float positive, float negative)
        {
            ReportManager.ReportEntry entry = new ReportManager.ReportEntry(type, 0, null);
            entry.accPositive = positive;
            entry.accNegative = negative;
            return entry;
        }

        private static Dictionary<string, object> Counter(ReportTelemetryReadResult result, string period, string counter)
        {
            return (Dictionary<string, object>)((Dictionary<string, object>)result.Counters[period])[counter];
        }

        private static Dictionary<string, object> QualityOf(ReportTelemetryReadResult result, string path)
        {
            Require(result.Quality.ContainsKey(path), "quality entry missing: " + path);
            return (Dictionary<string, object>)result.Quality[path];
        }

        private static bool Equal(object value, float expected)
        {
            return value is float number && number == expected;
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
