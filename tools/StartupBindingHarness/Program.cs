using System;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;

namespace StartupBindingHarness
{
    // Exercises StartupBinding's freshness rule against this process's own
    // start time: a binding left from an earlier run must not be accepted
    // (code review 2026-09-26).
    internal static class Program
    {
        private static int Main()
        {
            try
            {
                MethodInfo fresh = typeof(StartupBinding).GetMethod(
                    "IsFreshForThisProcess",
                    BindingFlags.Static | BindingFlags.NonPublic);
                if (fresh == null)
                {
                    throw new MissingMethodException("StartupBinding.IsFreshForThisProcess was not found");
                }
                DateTimeOffset start = new DateTimeOffset(Process.GetCurrentProcess().StartTime);
                Func<TimeSpan, string> stamp = offset =>
                    (start + offset).ToString("yyyy-MM-ddTHH:mm:ss.fffzzz", CultureInfo.InvariantCulture);
                var results = new JObject
                {
                    ["one_hour_before_start"] = (bool)fresh.Invoke(null, new object[] { stamp(TimeSpan.FromHours(-1)) }),
                    ["thirteen_hours_before_start"] = (bool)fresh.Invoke(null, new object[] { stamp(TimeSpan.FromHours(-13)) }),
                    ["ten_minutes_after_start"] = (bool)fresh.Invoke(null, new object[] { stamp(TimeSpan.FromMinutes(10)) }),
                    ["unparseable"] = (bool)fresh.Invoke(null, new object[] { "2026-09-09T12:00:00" + "x" }),
                };
                Console.WriteLine(new JObject { ["schema"] = "oni.startup-binding-harness.v1", ["fresh"] = results }
                    .ToString(Newtonsoft.Json.Formatting.None));
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
