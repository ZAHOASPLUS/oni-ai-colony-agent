using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace OniAiProbeMod.Api
{
    /// <summary>The Mod's own trouble log: ai-bridge/api-log.jsonl, one record per line.</summary>
    internal static class ApiLog
    {
        private const string FileName = "api-log.jsonl";
        private static readonly object Sync = new object();

        internal static string BridgeDirectory { get; private set; }

        internal static void Initialize()
        {
            string configured = Environment.GetEnvironmentVariable("ONI_AI_BRIDGE_DIR");
            BridgeDirectory = string.IsNullOrWhiteSpace(configured)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "Klei", "OxygenNotIncluded", "ai-bridge")
                : configured;
            Directory.CreateDirectory(BridgeDirectory);
        }

        internal static void Event(string name, IDictionary<string, object> fields = null)
        {
            // Built by hand so a log line can never depend on a serializer.
            StringBuilder line = new StringBuilder("{\"time_utc\":")
                .Append(Quote(System.DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)))
                .Append(",\"event\":")
                .Append(Quote(name));
            if (fields != null)
            {
                foreach (KeyValuePair<string, object> field in fields)
                {
                    line.Append(',').Append(Quote(field.Key)).Append(':').Append(Value(field.Value));
                }
            }
            line.Append("}\n");
            lock (Sync)
            {
                File.AppendAllText(Path.Combine(BridgeDirectory, FileName), line.ToString());
            }
        }

        internal static void Error(string stage, Exception error)
        {
            Error(stage, error.ToString());
        }

        internal static void Error(string stage, string message)
        {
            Event("error", new Dictionary<string, object> { { "stage", stage }, { "message", message } });
        }

        private static string Value(object value)
        {
            if (value == null)
            {
                return "null";
            }
            if (value is bool)
            {
                return (bool)value ? "true" : "false";
            }
            if (value is int || value is long || value is float || value is double)
            {
                return Convert.ToString(value, CultureInfo.InvariantCulture);
            }
            return Quote(value.ToString());
        }

        private static string Quote(string text)
        {
            return "\"" + text
                .Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\r", "\\r")
                .Replace("\n", "\\n") + "\"";
        }
    }
}
