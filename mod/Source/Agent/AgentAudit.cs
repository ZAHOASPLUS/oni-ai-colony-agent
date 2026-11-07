using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent
{
    /// <summary>
    /// A bounded, best-effort public activity log. Record and Dispose never do file I/O
    /// or wait for the writer. Data is summarized, never copied as a provider payload.
    /// This is not the durable, fail-closed rollback budget ledger.
    /// </summary>
    internal sealed class AgentAudit : IDisposable
    {
        public const int QueueCapacity = 256;
        public const int ViewCapacity = 200;
        public const long MaxFileBytes = 1024 * 1024;
        // Three files in total: the active file and two older files.
        public const int MaxFiles = 3;
        private const int MaxInputCharacters = 8192;
        private const int MaxToolDataCharacters = 128 * 1024;
        private const int MaxPreviewBytes = 4096;
        private const int MaxLineBytes = 8192;
        private const string Redacted = "[redacted]";
        private static readonly Encoding Utf8 = new UTF8Encoding(false);
        private static readonly TimeSpan RegexTimeout = TimeSpan.FromMilliseconds(40);
        private static readonly Regex Bearer = new Regex(
            @"\bBearer\s+[^\s""'<>;,\]}]+", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex SecretField = new Regex(
            @"(?:[""']?)(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|authorization|password|passwd|secret|client[ _-]?secret|private[ _-]?key|credential|token|cookie|set-cookie)(?:[""']?)\s*[:=]\s*(?:""(?:\\.|[^""\\\r\n])*""|'(?:\\.|[^'\\\r\n])*'|[^\s,;&}\]]+)",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex KeyLike = new Regex(
            @"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}", RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex PrivateKind = new Regex(
            @"reason|think|analysis|chain.?of.?thought|payload|raw|prompt|provider|request_body|response_body",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex PrivateContent = new Regex(
            @"<\s*/?\s*(?:think|analysis|reasoning)\b|[""'](?:reasoning(?:_content)?|chain_of_thought|choices|messages)[""']\s*:|(?:^|\n)\s*(?:analysis|reasoning|chain.?of.?thought)\s*:",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex PrivateField = new Regex(
            @"^(?:content|body|request|response|messages|choices|headers)$|requestbody|responsebody|reasoning|analysis|chainofthought|think|payload|provider|raw|prompt",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        private static readonly Regex SecretName = new Regex(
            @"key|token|password|passwd|secret|credential|authorization|cookie",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);

        private readonly object sync = new object();
        private readonly Queue<string> pending = new Queue<string>(QueueCapacity);
        private readonly Queue<string> view = new Queue<string>(ViewCapacity);
        private readonly string directory;
        private string apiKey;
        private bool stopping;
        private long dropped;
        private string status = "starting";

        public AgentAudit(string directory, string apiKey)
        {
            if (string.IsNullOrWhiteSpace(directory))
                throw new ArgumentException("An audit directory is required.", nameof(directory));
            // Path normalization does not access the filesystem.
            this.directory = Path.GetFullPath(directory);
            this.apiKey = apiKey ?? string.Empty;
            Thread writer = new Thread(WriteLoop) { IsBackground = true, Name = "ONI Agent Audit" };
            writer.Start();
        }

        /// <summary>starting, running, stopping, stopped, failed_io or failed_worker; no exception text.</summary>
        public string Status { get { lock (sync) return status; } }

        /// <summary>Records refused after shutdown, dropped on overflow, or lost on a writer failure.</summary>
        public long DroppedRecords { get { lock (sync) return dropped; } }

        /// <summary>Update the configured secret before starting requests with a replacement key.</summary>
        public void SetSecret(string value)
        {
            lock (sync) apiKey = value ?? string.Empty;
        }

        public void Record(AgentRuntimeEvent entry)
        {
            if (entry == null) return;
            string line;
            try
            {
                line = Format(entry.Kind, entry.Message, entry.SessionId, entry.Generation,
                    entry.ToolName, entry.ToolCallId, entry.Data,
                    entry.TimestampUtc.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture));
            }
            catch (Exception)
            {
                // Never log the failing object or the exception, either could contain a credential.
                line = FailureLine("audit_record_omitted", "Activity could not be safely summarized.");
            }
            Enqueue(line);
        }

        /// <summary>Only public activity messages belong here; raw HTTP bodies do not.</summary>
        public void Record(string kind, string message)
        {
            string line;
            try
            {
                line = Format(kind, message, null, 0, null, null, null,
                    DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture));
            }
            catch (Exception)
            {
                line = FailureLine("audit_record_omitted", "Activity could not be safely summarized.");
            }
            Enqueue(line);
        }

        /// <summary>A detached chronological copy, containing only the same sanitized lines as disk.</summary>
        public string[] Snapshot()
        {
            lock (sync) return view.ToArray();
        }

        public void Dispose()
        {
            lock (sync)
            {
                if (stopping) return;
                stopping = true;
                status = "stopping";
                Monitor.PulseAll(sync);
            }
            // The background writer drains at most 256 records, then closes its file.
            // A Unity lifecycle callback must not Join or Flush a possibly slow disk.
        }

        private void Enqueue(string line)
        {
            lock (sync)
            {
                if (stopping || pending.Count >= QueueCapacity)
                {
                    dropped++;
                    return;
                }
                pending.Enqueue(line);
                AddView(line);
                Monitor.Pulse(sync);
            }
        }

        private void AddView(string line)
        {
            if (view.Count == ViewCapacity) view.Dequeue();
            view.Enqueue(line);
        }

        private string Format(string kind, string message, string session, long generation,
            string tool, string call, string data, string utc)
        {
            bool privateContent = IsPrivate(kind, message);
            bool truncated = (kind?.Length ?? 0) > MaxInputCharacters || (message?.Length ?? 0) > MaxInputCharacters;
            JObject record = new JObject
            {
                ["utc"] = utc,
                ["kind"] = Sanitize(kind, 64, ref truncated),
                ["message"] = privateContent ? (truncated ? "[omitted: input limit]" : "[omitted: private or provider content]") : Sanitize(message, 768, ref truncated),
                ["session_id"] = Sanitize(session, 128, ref truncated),
                ["generation"] = generation,
                ["tool"] = Sanitize(tool, 80, ref truncated),
                ["tool_call_id"] = Sanitize(call, 128, ref truncated)
            };
            if (!string.IsNullOrEmpty(data))
                record["data_summary"] = SummarizeData(data, kind, privateContent, record, ref truncated);
            if (privateContent) record["content_omitted"] = true;
            if (truncated) record["truncated"] = true;
            string line = record.ToString(Formatting.None);
            if (Utf8.GetByteCount(line) + 1 <= MaxLineBytes) return line;
            record["message"] = "[omitted: audit line limit]";
            record["truncated"] = true;
            line = record.ToString(Formatting.None);
            if (Utf8.GetByteCount(line) + 1 <= MaxLineBytes) return line;
            record.Remove("data_summary");
            record.Remove("arguments_preview");
            record.Remove("result_preview");
            record["preview_omitted"] = "line_limit";
            record["truncated"] = true;
            return record.ToString(Formatting.None);
        }

        private static bool IsPrivate(string kind, string message)
        {
            if ((kind?.Length ?? 0) > MaxInputCharacters || (message?.Length ?? 0) > MaxInputCharacters)
                return true;
            if (PrivateKind.IsMatch(kind ?? string.Empty) || PrivateContent.IsMatch(message ?? string.Empty))
                return true;
            string trimmed = (message ?? string.Empty).TrimStart();
            // Embedded HTTP error bodies are payloads too, not public narrative summaries.
            return trimmed.IndexOf('{') >= 0 || trimmed.StartsWith("[", StringComparison.Ordinal);
        }

        private string Sanitize(string value, int limit, ref bool truncated)
        {
            if (string.IsNullOrEmpty(value)) return value;
            if (value.Length > MaxInputCharacters)
            {
                truncated = true;
                // Omit rather than slicing a credential in half before redaction.
                return "[omitted: " + value.Length.ToString(CultureInfo.InvariantCulture) + " characters]";
            }
            string secret;
            lock (sync) secret = apiKey;
            string safe = string.IsNullOrEmpty(secret) ? value : value.Replace(secret, Redacted);
            safe = Bearer.Replace(safe, "Bearer " + Redacted);
            safe = SecretField.Replace(safe, "[secret field redacted]");
            safe = KeyLike.Replace(safe, Redacted);
            StringBuilder clean = new StringBuilder(safe.Length);
            foreach (char character in safe)
                clean.Append(char.IsControl(character) ? ' ' : character);
            safe = clean.ToString();
            if (safe.Length <= limit) return safe;
            truncated = true;
            return safe.Substring(0, limit) + " [truncated]";
        }

        private JObject SummarizeData(string data, string kind, bool privateContent, JObject record, ref bool truncated)
        {
            JObject summary = new JObject { ["characters"] = data.Length, ["payload_omitted"] = true };
            // Only these exact runtime events carry game-tool arguments/results. No model
            // event or unknown event may use the preview route, even with valid JSON Data.
            bool toolEvent = kind == "tool_started" || kind == "tool_completed" || kind == "tool_completed_stale";
            if (privateContent || !toolEvent) return summary;
            if (data.Length > MaxToolDataCharacters)
            {
                truncated = true;
                summary["preview_omitted"] = "input_limit";
                return summary;
            }
            try
            {
                using (JsonTextReader reader = new JsonTextReader(new StringReader(data)))
                {
                    reader.DateParseHandling = DateParseHandling.None;
                    reader.MaxDepth = 24;
                    JToken parsed = JToken.ReadFrom(reader);
                    if (reader.Read()) throw new JsonReaderException("Additional audit data was omitted.");
                    JObject obj = parsed as JObject;
                    if (obj != null)
                    {
                        summary["fields"] = obj.Count;
                        if (obj["ok"]?.Type == JTokenType.Boolean) summary["ok"] = (bool)obj["ok"];
                        JObject error = obj["error"] as JObject;
                        if (error?["code"]?.Type == JTokenType.String)
                        {
                            string code = (string)error["code"];
                            summary["error_code"] = IsErrorCode(code) ? Sanitize(code, 64, ref truncated) : "[omitted]";
                        }
                        JObject result = obj["result"] as JObject;
                        if (result != null) summary["result_fields"] = result.Count;
                        JArray results = obj["result"] as JArray;
                        if (results != null) summary["result_items"] = results.Count;
                        if (kind != "tool_started")
                        {
                            JObject metadata = obj["_agent_dispatch"] as JObject;
                            CopyMetric(metadata, record, "execution_ms");
                            CopyMetric(metadata, record, "game_cycle");
                        }
                    }
                    else if (parsed is JArray array) summary["items"] = array.Count;
                    else summary["value_omitted"] = true;
                    int nodes = 256;
                    bool previewTruncated = false;
                    JToken preview = Preview(parsed, MaxPreviewBytes, 0, ref nodes, ref previewTruncated);
                    // This is a valid JSON subtree, never a substring of unsanitized JSON.
                    record[kind == "tool_started" ? "arguments_preview" : "result_preview"] = preview;
                    summary["preview_included"] = true;
                    if (previewTruncated)
                    {
                        record["preview_truncated"] = true;
                        truncated = true;
                    }
                }
            }
            catch (JsonException) { summary["unparsed"] = true; }
            return summary;
        }

        private static void CopyMetric(JObject metadata, JObject record, string name)
        {
            JToken value = metadata?[name];
            if (value == null || (value.Type != JTokenType.Integer && value.Type != JTokenType.Float)) return;
            double number;
            if (!double.TryParse(value.ToString(Formatting.None), NumberStyles.Float, CultureInfo.InvariantCulture, out number) ||
                double.IsNaN(number) || double.IsInfinity(number) || number < 0) return;
            record[name] = number;
        }

        private JToken Preview(JToken value, int byteLimit, int depth, ref int nodes, ref bool truncated)
        {
            if (--nodes < 0 || depth > 8 || byteLimit < 32)
            {
                truncated = true;
                return JValue.CreateNull();
            }
            if (value is JObject source)
            {
                JObject output = new JObject();
                int count = 0;
                foreach (JProperty property in source.Properties())
                {
                    if (count++ >= 32 || nodes <= 0) { truncated = true; break; }
                    string name = Sanitize(property.Name, 80, ref truncated);
                    // Renaming a redacted/long property must never overwrite a previous field.
                    if (output.Property(name) != null) { truncated = true; continue; }
                    int remaining = byteLimit - JsonBytes(output) - JsonBytes(new JValue(name)) - 2;
                    if (remaining < 32) { truncated = true; break; }
                    string normalized = property.Name.Replace("_", "").Replace("-", "").Replace(" ", "");
                    JToken child;
                    if (property.Name.Length > 256)
                    {
                        truncated = true;
                        child = new JValue("[omitted: field limit]");
                    }
                    else if (SecretName.IsMatch(normalized)) child = new JValue(Redacted);
                    else if (PrivateField.IsMatch(normalized)) child = new JValue("[omitted: private or provider content]");
                    else child = Preview(property.Value, remaining, depth + 1, ref nodes, ref truncated);
                    output[name] = child;
                    if (JsonBytes(output) > byteLimit)
                    {
                        output.Remove(name);
                        truncated = true;
                        break;
                    }
                }
                return output;
            }
            if (value is JArray array)
            {
                JArray output = new JArray();
                foreach (JToken item in array)
                {
                    if (output.Count >= 32 || nodes <= 0) { truncated = true; break; }
                    int remaining = byteLimit - JsonBytes(output) - 1;
                    if (remaining < 32) { truncated = true; break; }
                    output.Add(Preview(item, remaining, depth + 1, ref nodes, ref truncated));
                    if (JsonBytes(output) > byteLimit)
                    {
                        output.RemoveAt(output.Count - 1);
                        truncated = true;
                        break;
                    }
                }
                return output;
            }
            if (value.Type == JTokenType.String)
            {
                string text = (string)value;
                string safe = IsPrivate(null, text) ? "[omitted: private or provider content]" : Sanitize(text, 384, ref truncated);
                if (JsonBytes(new JValue(safe)) <= byteLimit) return new JValue(safe);
                truncated = true;
                int length = safe.Length;
                while (length > 0)
                {
                    length--;
                    if (length > 0 && char.IsHighSurrogate(safe[length - 1])) length--;
                    string shortened = safe.Substring(0, length) + " [truncated]";
                    if (JsonBytes(new JValue(shortened)) <= byteLimit) return new JValue(shortened);
                }
                return JValue.CreateNull();
            }
            if (value.Type == JTokenType.Null || value.Type == JTokenType.Boolean || value.Type == JTokenType.Integer || value.Type == JTokenType.Float)
            {
                if (value.Type == JTokenType.Float)
                {
                    double number = value.Value<double>();
                    if (double.IsNaN(number) || double.IsInfinity(number)) return JValue.CreateNull();
                }
                if (JsonBytes(value) <= byteLimit) return value.DeepClone();
            }
            truncated = true;
            return JValue.CreateNull();
        }

        private static int JsonBytes(JToken value) => Utf8.GetByteCount(value.ToString(Formatting.None));

        private static bool IsErrorCode(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length > 64) return false;
            foreach (char character in value)
                if (character != '_' && (character < 'a' || character > 'z') &&
                    (character < 'A' || character > 'Z') && (character < '0' || character > '9')) return false;
            return true;
        }

        private void WriteLoop()
        {
            FileStream output = null;
            bool inFlight = false;
            try
            {
                Directory.CreateDirectory(directory);
                string path = Path.Combine(directory, "agent-audit.jsonl");
                output = Open(path);
                lock (sync) { if (!stopping) status = "running"; }
                while (true)
                {
                    string line;
                    lock (sync)
                    {
                        while (pending.Count == 0 && !stopping) Monitor.Wait(sync);
                        if (pending.Count == 0) break;
                        line = pending.Dequeue();
                        inFlight = true;
                    }
                    byte[] bytes = Utf8.GetBytes(line + "\n");
                    if (output.Length + bytes.Length > MaxFileBytes)
                    {
                        output.Dispose();
                        output = null;
                        Rotate(path);
                        output = Open(path);
                    }
                    output.Write(bytes, 0, bytes.Length);
                    output.Flush();
                    inFlight = false;
                }
                output.Dispose();
                output = null;
                lock (sync) status = "stopped";
            }
            catch (Exception ex)
            {
                lock (sync)
                {
                    stopping = true;
                    status = ex is IOException || ex is UnauthorizedAccessException ? "failed_io" : "failed_worker";
                    dropped += pending.Count + (inFlight ? 1 : 0);
                    pending.Clear();
                    AddView(FailureLine("audit_stopped", "Background audit stopped; check its status and available storage."));
                }
            }
            finally
            {
                try { output?.Dispose(); }
                catch (Exception) { /* Never surface raw file paths or exception text to the UI. */ }
            }
        }

        private static FileStream Open(string path)
        {
            return new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read, 4096);
        }

        private static void Rotate(string path)
        {
            File.Delete(path + "." + (MaxFiles - 1).ToString(CultureInfo.InvariantCulture));
            for (int index = MaxFiles - 2; index >= 1; index--)
            {
                string previous = path + "." + index.ToString(CultureInfo.InvariantCulture);
                if (File.Exists(previous)) File.Move(previous, path + "." + (index + 1).ToString(CultureInfo.InvariantCulture));
            }
            if (File.Exists(path)) File.Move(path, path + ".1");
        }

        private static string FailureLine(string kind, string message)
        {
            return new JObject
            {
                ["utc"] = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                ["kind"] = kind,
                ["message"] = message
            }.ToString(Formatting.None);
        }
    }
}
