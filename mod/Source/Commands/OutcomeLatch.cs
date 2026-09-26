using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Commands
{
    // Durable outcome-unknown latch, shared with agent/command_queue.py
    // (_is_unresolved_outcome_unknown / has_operator_resolution).  A failed
    // mutating record latches further mutations unless the executor recorded a
    // definite pre-dispatch classification or an operator recorded an explicit
    // resolution with scripts/resolve_outcome_unknown.py.  A reload or a later
    // save never clears it.
    //
    // Failed records embed full before/after game states (megabytes), so the
    // reader streams through them and keeps only the few top-level fields the
    // latch needs.  Pure filesystem/JSON code without Unity dependencies so an
    // offline harness can exercise it.
    internal static class OutcomeLatch
    {
        internal const string OperatorResolutionSchema = "oni.operator-resolution.v1";

        private static readonly HashSet<string> DefiniteFailureClassifications =
            new HashSet<string>(StringComparer.Ordinal) { "behavior_fail", "rejected", "runtime_fail" };

        private static readonly HashSet<string> NonMutatingActions =
            new HashSet<string>(StringComparer.Ordinal) { "noop", "focus_note" };

        private static readonly HashSet<string> OperatorResolutionOutcomes =
            new HashSet<string>(StringComparer.Ordinal) { "effect_present", "effect_absent" };

        internal sealed class FailedRecord
        {
            internal string Id;
            internal string Action;
            internal string Classification;
            internal string ResultClassification;
            internal JObject OperatorResolution;
        }

        // Returns the id (or file stem) of the first latching failed record,
        // or null when every failed record is resolved or non-latching.  An
        // unreadable or malformed record latches: it cannot prove anything.
        internal static string FindUnresolvedFailure(string failedDirectory)
        {
            if (string.IsNullOrEmpty(failedDirectory) || !Directory.Exists(failedDirectory))
            {
                return null;
            }

            string[] files = Directory.GetFiles(failedDirectory, "*.json");
            Array.Sort(files, StringComparer.Ordinal);
            foreach (string path in files)
            {
                if (!TryReadFailedRecord(path, out FailedRecord record))
                {
                    return Path.GetFileNameWithoutExtension(path);
                }
                if (IsUnresolved(record))
                {
                    return string.IsNullOrEmpty(record.Id) ? Path.GetFileNameWithoutExtension(path) : record.Id;
                }
            }
            return null;
        }

        // Cheap change detector for failed/: names, sizes and write times.
        internal static string DirectorySignature(string failedDirectory)
        {
            if (string.IsNullOrEmpty(failedDirectory) || !Directory.Exists(failedDirectory))
            {
                return string.Empty;
            }

            string[] files = Directory.GetFiles(failedDirectory, "*.json");
            Array.Sort(files, StringComparer.Ordinal);
            StringBuilder builder = new StringBuilder();
            foreach (string path in files)
            {
                FileInfo info = new FileInfo(path);
                builder.Append(info.Name).Append('|')
                    .Append(info.Length.ToString(CultureInfo.InvariantCulture)).Append('|')
                    .Append(info.LastWriteTimeUtc.Ticks.ToString(CultureInfo.InvariantCulture)).Append('\n');
            }
            return builder.ToString();
        }

        internal static bool IsUnresolved(FailedRecord record)
        {
            if (record == null)
            {
                return true;
            }
            if (HasOperatorResolution(record))
            {
                return false;
            }
            if (record.Classification == "outcome_unknown" || record.ResultClassification == "outcome_unknown")
            {
                return true;
            }
            if (record.Action != null && NonMutatingActions.Contains(record.Action))
            {
                return false;
            }

            bool anyClassification = false;
            foreach (string classification in new[] { record.Classification, record.ResultClassification })
            {
                if (string.IsNullOrEmpty(classification))
                {
                    continue;
                }
                anyClassification = true;
                if (!DefiniteFailureClassifications.Contains(classification))
                {
                    return true;
                }
            }
            // A missing classification (for example a raw executing claim moved
            // to failed/) cannot prove where dispatch stopped.
            return !anyClassification;
        }

        private static bool HasOperatorResolution(FailedRecord record)
        {
            JObject value = record.OperatorResolution;
            if (value == null)
            {
                return false;
            }

            string reason = StringValue(value["reason"]);
            string resolvedAt = StringValue(value["resolved_at"]);
            return StringValue(value["schema"]) == OperatorResolutionSchema
                && record.Id != null
                && StringValue(value["command_id"]) == record.Id
                && StringValue(value["resolved_by"]) == "local_human"
                && OperatorResolutionOutcomes.Contains(StringValue(value["observed_outcome"]) ?? string.Empty)
                && !string.IsNullOrWhiteSpace(reason)
                && !string.IsNullOrEmpty(resolvedAt)
                && DateTimeOffset.TryParse(resolvedAt, CultureInfo.InvariantCulture, DateTimeStyles.None, out _);
        }

        internal static bool TryReadFailedRecord(string path, out FailedRecord record)
        {
            record = null;
            try
            {
                using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (StreamReader text = new StreamReader(stream, new UTF8Encoding(false), true))
                using (JsonTextReader reader = new JsonTextReader(text))
                {
                    reader.DateParseHandling = DateParseHandling.None;
                    if (!reader.Read() || reader.TokenType != JsonToken.StartObject)
                    {
                        return false;
                    }

                    FailedRecord result = new FailedRecord();
                    HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
                    while (reader.Read())
                    {
                        if (reader.TokenType == JsonToken.EndObject)
                        {
                            if (reader.Read())
                            {
                                return false;  // trailing content after the object
                            }
                            record = result;
                            return true;
                        }
                        if (reader.TokenType != JsonToken.PropertyName)
                        {
                            return false;
                        }

                        string name = (string)reader.Value;
                        if (!seen.Add(name) || !reader.Read())
                        {
                            return false;  // duplicate keys cannot prove one meaning
                        }
                        switch (name)
                        {
                            case "id":
                                result.Id = ReadStringOrSkip(reader);
                                break;
                            case "action":
                                result.Action = ReadStringOrSkip(reader);
                                break;
                            case "classification":
                                result.Classification = ReadStringOrSkip(reader);
                                break;
                            case "result":
                                if (!TryReadResultClassification(reader, out result.ResultClassification))
                                {
                                    return false;
                                }
                                break;
                            case "operator_resolution":
                                if (reader.TokenType == JsonToken.StartObject)
                                {
                                    result.OperatorResolution = JObject.Load(reader);
                                }
                                else
                                {
                                    reader.Skip();
                                }
                                break;
                            default:
                                reader.Skip();
                                break;
                        }
                    }
                    return false;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static bool TryReadResultClassification(JsonTextReader reader, out string classification)
        {
            classification = null;
            if (reader.TokenType != JsonToken.StartObject)
            {
                reader.Skip();
                return true;
            }

            HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
            while (reader.Read())
            {
                if (reader.TokenType == JsonToken.EndObject)
                {
                    return true;
                }
                if (reader.TokenType != JsonToken.PropertyName)
                {
                    return false;
                }

                string name = (string)reader.Value;
                if (!seen.Add(name) || !reader.Read())
                {
                    return false;
                }
                if (name == "classification")
                {
                    classification = ReadStringOrSkip(reader);
                }
                else
                {
                    reader.Skip();  // before_state/after_state are megabytes
                }
            }
            return false;
        }

        private static string ReadStringOrSkip(JsonTextReader reader)
        {
            if (reader.TokenType == JsonToken.String)
            {
                return (string)reader.Value;
            }
            reader.Skip();
            return null;
        }

        private static string StringValue(JToken token)
        {
            return token != null && token.Type == JTokenType.String ? (string)token : null;
        }
    }
}
