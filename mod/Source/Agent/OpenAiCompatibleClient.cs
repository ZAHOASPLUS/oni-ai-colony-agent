using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent
{
    public interface IAgentModelClient
    {
        // Callers transfer read ownership of these arrays until the returned Task completes.
        Task<JObject> CompleteAsync(JArray messages, JArray tools, CancellationToken cancellationToken);
    }

    public sealed class AgentModelException : Exception
    {
        public string Code { get; }
        public int? StatusCode { get; }

        internal AgentModelException(string code, int? statusCode = null)
            : base("Model request failed (" + code + (statusCode.HasValue ? "; HTTP " + statusCode.Value : "") + ").")
        {
            Code = code;
            StatusCode = statusCode;
        }
    }

    public sealed class ConnectionTestResult
    {
        public bool Success { get; }
        public Uri FinalEndpoint { get; }
        public string Model { get; }

        internal ConnectionTestResult(Uri endpoint, string model)
        {
            Success = true;
            FinalEndpoint = endpoint;
            Model = model;
        }
    }

    /// <summary>
    /// Non-streaming Chat Completions. One bounded request at a time, no automatic
    /// redirects, no raw network/provider exceptions, no Unity objects, no logging.
    /// </summary>
    public sealed class OpenAiCompatibleClient : IAgentModelClient, IDisposable
    {
        private const string EchoTool = "oni_connection_echo";
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private readonly AgentConfig config;
        private readonly HttpClient http;
        private readonly SemaphoreSlim singleRequest = new SemaphoreSlim(1, 1);
        private readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        private string apiKey;
        private int disposed;
        private JObject lastUsage;

        public Uri FinalEndpoint { get; }
        public string LastFinishReason { get; private set; }
        public JObject LastUsage { get { return lastUsage == null ? null : (JObject)lastUsage.DeepClone(); } }

        public OpenAiCompatibleClient(AgentConfig config, string apiKey)
            : this(config, apiKey, null) { }

        // For a linked-source harness only. Production always constructs its own safe handler.
        internal OpenAiCompatibleClient(AgentConfig config, string apiKey, HttpMessageHandler handler)
        {
            if (config == null) throw new ArgumentNullException(nameof(config));
            this.config = config.Snapshot();
            this.config.Validate(true);
            CredentialStore.ValidateApiKey(apiKey);
            this.apiKey = apiKey;
            FinalEndpoint = this.config.GetChatCompletionsEndpoint();
            if (handler == null)
            {
                handler = new HttpClientHandler
                {
                    AllowAutoRedirect = false,
                    UseCookies = false,
                    UseDefaultCredentials = false,
                    // A developer loopback request must not send its key through a proxy.
                    UseProxy = !AgentConfig.IsLoopbackHost(FinalEndpoint)
                };
            }
            http = new HttpClient(handler, true) { Timeout = Timeout.InfiniteTimeSpan };
        }

        public Task<JObject> CompleteAsync(JArray messages, JArray tools, CancellationToken cancellationToken)
        {
            // Even JSON serialization/validation stays off Unity's main thread.
            return Task.Run(() => CompleteCoreAsync(messages, tools, null, cancellationToken), cancellationToken);
        }

        public Task<ConnectionTestResult> TestConnectionAsync(CancellationToken cancellationToken)
        {
            return Task.Run(async () =>
            {
                string nonce = "oni-echo-" + Guid.NewGuid().ToString("N");
                JArray testTools = new JArray(new JObject
                {
                    ["type"] = "function",
                    ["function"] = new JObject
                    {
                        ["name"] = EchoTool,
                        ["description"] = "Harmless connection test. Returns the supplied text without using the game.",
                        ["parameters"] = new JObject
                        {
                            ["type"] = "object",
                            ["properties"] = new JObject { ["text"] = new JObject { ["type"] = "string", ["enum"] = new JArray(nonce) } },
                            ["required"] = new JArray("text"),
                            ["additionalProperties"] = false
                        }
                    }
                });
                JArray messages = new JArray(new JObject
                {
                    ["role"] = "user",
                    ["content"] = "Test tool calling: call oni_connection_echo with text " + nonce + ". After the tool result, reply with only the returned text."
                });
                JObject choice = new JObject { ["type"] = "function", ["function"] = new JObject { ["name"] = EchoTool } };
                JObject assistant = await CompleteCoreAsync(messages, testTools, choice, cancellationToken).ConfigureAwait(false);
                JArray calls = assistant["tool_calls"] as JArray;
                if (calls == null || calls.Count != 1 || (string)calls[0]["function"]?["name"] != EchoTool)
                    throw new AgentModelException("connection_test_missing_echo_call");
                JObject arguments = ParseObject((string)calls[0]["function"]["arguments"]);
                if (arguments.Count != 1 || arguments["text"]?.Type != JTokenType.String || (string)arguments["text"] != nonce)
                    throw new AgentModelException("connection_test_invalid_echo_arguments");
                messages.Add(assistant);
                messages.Add(new JObject
                {
                    ["role"] = "tool",
                    ["tool_call_id"] = calls[0]["id"].DeepClone(),
                    ["content"] = new JObject { ["text"] = nonce }.ToString(Formatting.None)
                });
                JObject followup = await CompleteCoreAsync(messages, testTools, new JValue("none"), cancellationToken).ConfigureAwait(false);
                if (followup["tool_calls"] != null || ((string)followup["content"])?.Trim() != nonce)
                    throw new AgentModelException("connection_test_invalid_echo_result");
                return new ConnectionTestResult(FinalEndpoint, config.Model);
            }, cancellationToken);
        }

        private async Task<JObject> CompleteCoreAsync(JArray messages, JArray tools, JToken toolChoice, CancellationToken cancellationToken)
        {
            if (Volatile.Read(ref disposed) != 0) throw new AgentModelException("client_disposed");
            bool acquired = false;
            using (CancellationTokenSource deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token))
            {
                // This deadline covers queue wait, every attempt and every backoff, not just one HTTP send.
                // Each attempt gets the full per-attempt timeout; backoffs share the total.
                // (The first version gave all attempts and backoffs one timeout together, so a
                // single slow reply left no time to retry.)
                deadline.CancelAfter(TimeSpan.FromSeconds(config.RequestTimeoutSeconds * (config.MaxRetries + 1)));
                CancellationToken token = deadline.Token;
                try
                {
                    await singleRequest.WaitAsync(token).ConfigureAwait(false);
                    acquired = true;
                    lastUsage = null;
                    LastFinishReason = null;
                    byte[] payload = BuildPayload(messages, tools, toolChoice);
                    token.ThrowIfCancellationRequested();
                    for (int attempt = 0; attempt <= config.MaxRetries; attempt++)
                    {
                        TimeSpan retryDelay;
                        try
                        {
                            using (HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, FinalEndpoint))
                            {
                                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
                                request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
                                request.Content = new ByteArrayContent(payload);
                                request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json") { CharSet = "utf-8" };
                                using (CancellationTokenSource attemptDeadline = CancellationTokenSource.CreateLinkedTokenSource(token))
                                {
                                attemptDeadline.CancelAfter(TimeSpan.FromSeconds(config.RequestTimeoutSeconds));
                                CancellationToken attemptToken = attemptDeadline.Token;
                                using (HttpResponseMessage response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, attemptToken).ConfigureAwait(false))
                                {
                                    int status = (int)response.StatusCode;
                                    if (status >= 300 && status < 400)
                                        throw new AgentModelException("redirect_rejected", status);
                                    if (status >= 200 && status < 300)
                                    {
                                        string body = await ReadBoundedAsync(response.Content, attemptToken).ConfigureAwait(false);
                                        token.ThrowIfCancellationRequested();
                                        JObject assistant = ParseAssistant(body);
                                        token.ThrowIfCancellationRequested();
                                        return assistant;
                                    }
                                    // Do not read or expose error bodies: gateways can echo Authorization.
                                    if (!IsRetryable(status) || attempt == config.MaxRetries)
                                        throw new AgentModelException("http_error", status);
                                    retryDelay = GetRetryDelay(response.Headers.RetryAfter, attempt);
                                }
                                }
                            }
                        }
                        catch (HttpRequestException)
                        {
                            if (attempt == config.MaxRetries) throw new AgentModelException("network_error");
                            retryDelay = GetRetryDelay(null, attempt);
                        }
                        catch (OperationCanceledException) when (!token.IsCancellationRequested)
                        {
                            // Only this attempt's own timeout fired: retry like a network error.
                            if (attempt == config.MaxRetries) throw new AgentModelException("timeout");
                            retryDelay = GetRetryDelay(null, attempt);
                        }
                        await Task.Delay(retryDelay, token).ConfigureAwait(false);
                    }
                    throw new AgentModelException("retry_limit");
                }
                catch (OperationCanceledException)
                {
                    if (cancellationToken.IsCancellationRequested) throw new OperationCanceledException(cancellationToken);
                    throw new AgentModelException(lifetime.IsCancellationRequested ? "client_disposed" : "timeout");
                }
                catch (AgentModelException) { throw; }
                catch
                {
                    // Disposing a canceled response stream can surface as IOException.
                    if (cancellationToken.IsCancellationRequested) throw new OperationCanceledException(cancellationToken);
                    if (deadline.IsCancellationRequested)
                        throw new AgentModelException(lifetime.IsCancellationRequested ? "client_disposed" : "timeout");
                    // No InnerException: exception messages can contain provider bodies, URLs or headers.
                    throw new AgentModelException("request_failed");
                }
                finally
                {
                    if (acquired) singleRequest.Release();
                }
            }
        }

        private byte[] BuildPayload(JArray messages, JArray tools, JToken toolChoice)
        {
            if (messages == null || messages.Count == 0 || messages.Count > config.MaxContextMessages)
                throw new AgentModelException("invalid_message_count");
            if (tools == null || tools.Count > 256) throw new AgentModelException("invalid_tool_count");
            ValidateInputTree(messages, Math.Min(config.MaxContextCharacters, config.MaxRequestBytes));
            ValidateInputTree(tools, config.MaxRequestBytes);
            using (CappedMemoryStream bytes = new CappedMemoryStream(config.MaxRequestBytes))
            {
                using (StreamWriter text = new StreamWriter(bytes, StrictUtf8, 1024, true))
                using (JsonTextWriter json = new JsonTextWriter(text) { Formatting = Formatting.None })
                {
                    // Write directly: assigning arrays into another JObject can trigger a
                    // full DeepClone before the byte limit has a chance to run.
                    json.WriteStartObject();
                    json.WritePropertyName("model");
                    json.WriteValue(config.Model);
                    json.WritePropertyName("stream");
                    json.WriteValue(false);
                    json.WritePropertyName("messages");
                    messages.WriteTo(json);
                    // max_tokens is rejected by current OpenAI reasoning models.
                    json.WritePropertyName("max_completion_tokens");
                    json.WriteValue(config.MaxCompletionTokens);
                    if (tools.Count > 0)
                    {
                        json.WritePropertyName("tools");
                        tools.WriteTo(json);
                        json.WritePropertyName("tool_choice");
                        (toolChoice ?? new JValue("auto")).WriteTo(json);
                    }
                    json.WriteEndObject();
                    json.Flush();
                }
                return bytes.ToArray();
            }
        }

        private static void ValidateInputTree(JToken root, int characterLimit)
        {
            // Traverse iteratively, before Json.NET's recursive writer, to reject deep
            // or very large inputs without allocating copies or overflowing the stack.
            JToken current = root;
            int depth = 0;
            long characters = 0;
            while (current != null)
            {
                if (depth > 64) throw new AgentModelException("input_too_deep");
                characters++;
                if (current is JProperty property) characters += property.Name.Length;
                else if (current.Type == JTokenType.String) characters += ((string)current).Length;
                else if (current.Type != JTokenType.Object && current.Type != JTokenType.Array &&
                    current.Type != JTokenType.Integer && current.Type != JTokenType.Float &&
                    current.Type != JTokenType.Boolean && current.Type != JTokenType.Null)
                    throw new AgentModelException("unsupported_input_json");
                if (characters > characterLimit) throw new AgentModelException("request_too_large");
                JContainer container = current as JContainer;
                if (container != null && container.First != null)
                {
                    current = container.First;
                    depth++;
                    continue;
                }
                while (current != root && current.Next == null)
                {
                    current = current.Parent;
                    depth--;
                }
                if (current == root) break;
                current = current.Next;
            }
        }

        private async Task<string> ReadBoundedAsync(HttpContent content, CancellationToken token)
        {
            if (content == null) throw new AgentModelException("empty_response");
            if (content.Headers.ContentLength.HasValue && content.Headers.ContentLength.Value > config.MaxResponseBytes)
                throw new AgentModelException("response_too_large");
            using (Stream stream = await content.ReadAsStreamAsync().ConfigureAwait(false))
            using (MemoryStream output = new MemoryStream())
            using (token.Register(() => { try { stream.Dispose(); } catch { } }))
            {
                byte[] buffer = new byte[8192];
                while (true)
                {
                    token.ThrowIfCancellationRequested();
                    int count = await stream.ReadAsync(buffer, 0, buffer.Length, token).ConfigureAwait(false);
                    if (count == 0) break;
                    if (output.Length + count > config.MaxResponseBytes) throw new AgentModelException("response_too_large");
                    output.Write(buffer, 0, count);
                }
                token.ThrowIfCancellationRequested();
                try { return StrictUtf8.GetString(output.ToArray()); }
                catch (DecoderFallbackException) { throw new AgentModelException("invalid_response_encoding"); }
            }
        }

        private JObject ParseAssistant(string body)
        {
            JObject root = ParseObject(body);
            JArray choices = root["choices"] as JArray;
            JObject choice = choices != null && choices.Count > 0 ? choices[0] as JObject : null;
            JObject message = choice?["message"] as JObject;
            string finish = choice?["finish_reason"]?.Type == JTokenType.String ? (string)choice["finish_reason"] : null;
            if (message == null || (string)message["role"] != "assistant") throw new AgentModelException("invalid_assistant_message");
            // Gateways differ: "stop" with tool calls, null, "end_turn".  Only a filtered
            // reply is unusable; a length cut keeps what arrived (a truncated call's
            // arguments fail to parse and go back to the model as an error).
            if (finish == "content_filter") throw new AgentModelException("incomplete_response");
            JToken content = message["content"];
            if (content != null && content.Type != JTokenType.Null && content.Type != JTokenType.String)
                throw new AgentModelException("unsupported_content");
            JObject normalized = new JObject { ["role"] = "assistant", ["content"] = content?.DeepClone() ?? JValue.CreateNull() };
            JArray calls = message["tool_calls"] as JArray;
            if (message["tool_calls"] != null && message["tool_calls"].Type != JTokenType.Null && calls == null)
                throw new AgentModelException("invalid_tool_calls");
            if (calls != null && calls.Count > 0)
            {
                // The runtime trims an oversized batch and repairs ids; keep every call here.
                JArray normalizedCalls = new JArray();
                HashSet<string> identifiers = new HashSet<string>(StringComparer.Ordinal);
                foreach (JToken call in calls)
                {
                    JObject function = call["function"] as JObject;
                    string id = call["id"]?.Type == JTokenType.String ? (string)call["id"] : null;
                    string name = function?["name"]?.Type == JTokenType.String ? (string)function["name"] : null;
                    string arguments = function?["arguments"]?.Type == JTokenType.String ? (string)function["arguments"] : null;
                    // Repair instead of rejecting the whole reply: the runtime answers an
                    // unknown name or unparsable arguments with an error to the model.
                    if (!ValidIdentifier(id, 128, false) || !identifiers.Add(id)) id = null;
                    if (!ValidIdentifier(name, 64, true)) name = "invalid_tool_name";
                    if (arguments == null) arguments = "{}";
                    normalizedCalls.Add(new JObject
                    {
                        ["id"] = id,
                        ["type"] = "function",
                        ["function"] = new JObject { ["name"] = name, ["arguments"] = arguments }
                    });
                }
                normalized["tool_calls"] = normalizedCalls;
            }
            else if (string.IsNullOrWhiteSpace((string)content))
                throw new AgentModelException(finish == "length" ? "incomplete_response" : "empty_assistant_message");
            LastFinishReason = finish;
            JObject usage = root["usage"] as JObject;
            if (usage != null)
            {
                JObject safeUsage = new JObject();
                foreach (string name in new[] { "prompt_tokens", "completion_tokens", "total_tokens" })
                    if (usage[name]?.Type == JTokenType.Integer && (long)usage[name] >= 0) safeUsage[name] = usage[name].DeepClone();
                lastUsage = safeUsage;
            }
            // Deliberately omit provider-specific reasoning/thinking fields.
            return normalized;
        }

        private static JObject ParseObject(string value)
        {
            try
            {
                using (StringReader text = new StringReader(value ?? ""))
                using (JsonTextReader json = new JsonTextReader(text) { MaxDepth = 64, DateParseHandling = DateParseHandling.None })
                {
                    JObject parsed = JObject.Load(json);
                    if (json.Read()) throw new AgentModelException("invalid_response_json");
                    return parsed;
                }
            }
            catch (AgentModelException) { throw; }
            catch { throw new AgentModelException("invalid_response_json"); }
        }

        private static bool ValidIdentifier(string value, int maximumLength, bool functionName)
        {
            if (string.IsNullOrEmpty(value) || value.Length > maximumLength) return false;
            foreach (char character in value)
            {
                if (character < 33 || character > 126) return false;
                if (functionName && !((character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') ||
                    (character >= '0' && character <= '9') || character == '_' || character == '-')) return false;
            }
            return true;
        }

        private static bool IsRetryable(int status)
        {
            return status == 408 || status == 429 || status == 500 || status == 502 || status == 503 || status == 504;
        }

        private TimeSpan GetRetryDelay(RetryConditionHeaderValue retryAfter, int attempt)
        {
            double seconds = Math.Pow(2, attempt) * 0.5;
            if (retryAfter?.Delta != null) seconds = retryAfter.Delta.Value.TotalSeconds;
            else if (retryAfter?.Date != null) seconds = (retryAfter.Date.Value - DateTimeOffset.UtcNow).TotalSeconds;
            return TimeSpan.FromSeconds(Math.Max(0, Math.Min(config.MaxRetryDelaySeconds, seconds)));
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref disposed, 1) != 0) return;
            lifetime.Cancel();
            http.Dispose();
            // Immutable .NET strings cannot be wiped; drop the reference and never serialize it.
            apiKey = null;
        }

        private sealed class CappedMemoryStream : MemoryStream
        {
            private readonly int limit;
            public CappedMemoryStream(int limit) { this.limit = limit; }
            public override void Write(byte[] buffer, int offset, int count)
            {
                if (Length + count > limit) throw new AgentModelException("request_too_large");
                base.Write(buffer, offset, count);
            }
            public override void WriteByte(byte value)
            {
                if (Length >= limit) throw new AgentModelException("request_too_large");
                base.WriteByte(value);
            }
        }
    }
}
