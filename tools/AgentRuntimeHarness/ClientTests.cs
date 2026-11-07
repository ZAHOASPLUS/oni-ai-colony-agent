using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent.Harness
{
    /// <summary>Pure fake HTTP transport tests. No model service, real key or game is used.</summary>
    public static class OpenAiClientTests
    {
        private const string FakeKey = "harness-not-a-real-key";

        public static async Task RunAll()
        {
            EndpointValidation();
            await CompletionAndMetadata().ConfigureAwait(false);
            await RejectRedirectAndSanitizeErrors().ConfigureAwait(false);
            await InputAndResponseLimits().ConfigureAwait(false);
            await RetryBounds().ConfigureAwait(false);
            await CancellationAndDeadline().ConfigureAwait(false);
            await EchoRoundTrip().ConfigureAwait(false);
            await RejectWrongEchoTool().ConfigureAwait(false);
            CredentialsFailClosed();
            Console.WriteLine("PASS client: endpoint, credentials, bounded HTTP, retries, cancellation, echo tool-call round-trip (fake transport)");
        }

        private static AgentConfig Config()
        {
            return new AgentConfig { BaseUrl = "https://model.example/custom/v1/", Model = "explicit-model", MaxRetries = 0 };
        }

        private static JArray Messages(string content = "hello")
        {
            return new JArray(new JObject { ["role"] = "user", ["content"] = content });
        }

        private static void EndpointValidation()
        {
            new AgentConfig().Validate();
            AgentConfig config = Config();
            Check(config.GetChatCompletionsEndpoint().AbsoluteUri == "https://model.example/custom/v1/chat/completions", "explicit prefix preserved");
            config.BaseUrl = "https://model.example/";
            Check(config.GetChatCompletionsEndpoint().AbsoluteUri == "https://model.example/chat/completions", "no guessed v1");
            config.BaseUrl = "https://model.example/custom/chat/completions///";
            Check(config.GetChatCompletionsEndpoint().AbsoluteUri == "https://model.example/custom/chat/completions", "final endpoint not duplicated");
            foreach (string invalid in new[] { "http://model.example/v1", "ftp://localhost/v1", "https://user:secret@model.example/v1", "https://model.example/v1?key=x", "https://model.example/v1#fragment", "https://model.example/%0d%0aevil", "https://model.example\\bad" })
            {
                config.BaseUrl = invalid;
                ExpectArgument(() => config.GetChatCompletionsEndpoint());
            }
            config.BaseUrl = "http://127.0.0.1:8080/v1";
            ExpectArgument(() => config.GetChatCompletionsEndpoint());
            config.AllowLocalhostHttp = true;
            foreach (string local in new[] { "http://127.0.0.1:8080/v1", "http://localhost:8080", "http://[::1]:8080" })
            {
                config.BaseUrl = local;
                Check(config.GetChatCompletionsEndpoint().Scheme == "http", "explicit loopback accepted");
            }
            foreach (string remote in new[] { "http://127.0.0.1.evil.example", "http://localhost.evil.example", "http://192.168.1.10", "http://0.0.0.0" })
            {
                config.BaseUrl = remote;
                ExpectArgument(() => config.GetChatCompletionsEndpoint());
            }
            config = Config();
            config.MaxRetries = 100;
            ExpectArgument(() => config.Validate());
            AgentConfig copy = Config();
            AgentConfig snapshot = copy.Snapshot();
            copy.Model = "changed";
            Check(snapshot.Model == "explicit-model", "immutable config snapshot");
            ExpectArgument(() => new OpenAiCompatibleClient(Config(), "bad\r\nAuthorization: injected"));
            Check(!JsonConvert.SerializeObject(Config()).Contains("ApiKey"), "ordinary config has no key field");
        }

        private static async Task CompletionAndMetadata()
        {
            AgentConfig config = Config();
            FakeHandler handler = new FakeHandler(async (request, token) =>
            {
                Check(request.Method == HttpMethod.Post, "POST only");
                Check(request.RequestUri.AbsoluteUri == "https://model.example/custom/v1/chat/completions", "final endpoint exact");
                Check(request.Headers.Authorization.Scheme == "Bearer" && request.Headers.Authorization.Parameter == FakeKey, "authorization header");
                JObject body = JObject.Parse(await request.Content.ReadAsStringAsync().ConfigureAwait(false));
                Check((string)body["model"] == "explicit-model" && !(bool)body["stream"], "snapshot model and non-streaming");
                Check((int)body["max_tokens"] == 4096, "completion limit sent");
                JObject response = ResponseObject("氧气正常");
                response["choices"][0]["message"]["reasoning_content"] = "must not be surfaced";
                response["usage"] = new JObject { ["prompt_tokens"] = 3, ["completion_tokens"] = 4, ["total_tokens"] = 7, ["secret_debug"] = FakeKey };
                return Json(response);
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, handler))
            {
                config.Model = "changed-after-client-creation";
                JObject result = await client.CompleteAsync(Messages(), new JArray(), CancellationToken.None).ConfigureAwait(false);
                Check((string)result["content"] == "氧气正常", "UTF-8 completion");
                Check(result["reasoning_content"] == null, "reasoning omitted");
                Check(client.LastFinishReason == "stop" && (int)client.LastUsage["total_tokens"] == 7, "metadata parsed");
                Check(client.LastUsage["secret_debug"] == null, "usage allowlist");
                Check(handler.Calls == 1, "one successful request");
            }
        }

        private static async Task RejectRedirectAndSanitizeErrors()
        {
            FakeHandler redirect = new FakeHandler((request, token) =>
            {
                HttpResponseMessage response = new HttpResponseMessage(HttpStatusCode.TemporaryRedirect);
                response.Headers.Location = new Uri("https://other.example/steal");
                return Task.FromResult(response);
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, redirect))
                await ExpectCode("redirect_rejected", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            Check(redirect.Calls == 1, "redirect not followed/retried");
            FakeHandler error = new FakeHandler((request, token) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Unauthorized)
            {
                Content = new StringContent("echoed secret " + FakeKey)
            }));
            AgentConfig config = Config();
            config.MaxRetries = 5;
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, error))
            {
                AgentModelException failure = await ExpectCode("http_error", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
                Check(failure.StatusCode == 401 && failure.InnerException == null && !failure.ToString().Contains(FakeKey), "provider error sanitized");
            }
            Check(error.Calls == 1, "auth error never retried");
            FakeHandler network = new FakeHandler((request, token) => throw new HttpRequestException("Authorization: " + FakeKey));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, network))
            {
                AgentModelException failure = await ExpectCode("network_error", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
                Check(failure.InnerException == null && !failure.ToString().Contains(FakeKey), "network error sanitized");
            }
        }

        private static async Task InputAndResponseLimits()
        {
            AgentConfig config = Config();
            config.MaxRequestBytes = 1024;
            FakeHandler unused = new FakeHandler((request, token) => Task.FromResult(Json(ResponseObject("ok"))));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, unused))
                await ExpectCode("request_too_large", () => client.CompleteAsync(Messages(new string('a', 2048)), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            Check(unused.Calls == 0, "oversized request never sent");
            JArray deeplyNested = new JArray();
            JArray level = deeplyNested;
            for (int depth = 0; depth < 70; depth++)
            {
                JArray next = new JArray();
                level.Add(next);
                level = next;
            }
            FakeHandler deepUnused = new FakeHandler((request, token) => Task.FromResult(Json(ResponseObject("ok"))));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, deepUnused))
                await ExpectCode("input_too_deep", () => client.CompleteAsync(deeplyNested, new JArray(), CancellationToken.None)).ConfigureAwait(false);
            Check(deepUnused.Calls == 0, "deep input never sent");
            config = Config();
            config.MaxResponseBytes = 1024;
            FakeHandler oversized = new FakeHandler((request, token) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(new string('a', 2048))
            }));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, oversized))
                await ExpectCode("response_too_large", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            FakeHandler chunked = new FakeHandler((request, token) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StreamContent(new NonSeekableStream(Encoding.UTF8.GetBytes(new string('b', 2048))))
            }));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, chunked))
                await ExpectCode("response_too_large", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            FakeHandler malformed = new FakeHandler((request, token) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("{not json " + FakeKey)
            }));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, malformed))
                await ExpectCode("invalid_response_json", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            FakeHandler truncated = new FakeHandler((request, token) =>
            {
                JObject response = ResponseObject("partial");
                response["choices"][0]["finish_reason"] = "length";
                return Task.FromResult(Json(response));
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, truncated))
                await ExpectCode("incomplete_response", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            FakeHandler duplicateCalls = new FakeHandler((request, token) =>
            {
                JObject response = ToolResponse("echo", "test");
                JArray calls = (JArray)response["choices"][0]["message"]["tool_calls"];
                calls.Add(calls[0].DeepClone());
                return Task.FromResult(Json(response));
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, duplicateCalls))
                await ExpectCode("invalid_tool_call", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
        }

        private static async Task RetryBounds()
        {
            AgentConfig config = Config();
            config.MaxRetries = 2;
            int calls = 0;
            FakeHandler eventual = new FakeHandler((request, token) =>
            {
                if (++calls == 3) return Task.FromResult(Json(ResponseObject("ok")));
                HttpResponseMessage response = new HttpResponseMessage((HttpStatusCode)429);
                response.Headers.RetryAfter = new RetryConditionHeaderValue(TimeSpan.Zero);
                return Task.FromResult(response);
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, eventual))
                await client.CompleteAsync(Messages(), new JArray(), CancellationToken.None).ConfigureAwait(false);
            Check(eventual.Calls == 3, "429 retry can succeed");
            FakeHandler limited = new FakeHandler((request, token) =>
            {
                HttpResponseMessage response = new HttpResponseMessage((HttpStatusCode)429);
                response.Headers.RetryAfter = new RetryConditionHeaderValue(TimeSpan.Zero);
                return Task.FromResult(response);
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, limited))
                await ExpectCode("http_error", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            Check(limited.Calls == 3, "finite retry budget");
        }

        private static async Task CancellationAndDeadline()
        {
            FakeHandler pending = new FakeHandler(async (request, token) =>
            {
                await Task.Delay(Timeout.Infinite, token).ConfigureAwait(false);
                return Json(ResponseObject("unreachable"));
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, pending))
            using (CancellationTokenSource cancellation = new CancellationTokenSource())
            {
                cancellation.CancelAfter(50);
                try
                {
                    await client.CompleteAsync(Messages(), new JArray(), cancellation.Token).ConfigureAwait(false);
                    throw new Exception("Expected caller cancellation.");
                }
                catch (OperationCanceledException) { }
            }
            AgentConfig config = Config();
            config.RequestTimeoutSeconds = 1;
            config.MaxRetries = 5;
            config.MaxRetryDelaySeconds = 60;
            FakeHandler longBackoff = new FakeHandler((request, token) =>
            {
                HttpResponseMessage response = new HttpResponseMessage((HttpStatusCode)429);
                response.Headers.RetryAfter = new RetryConditionHeaderValue(TimeSpan.FromDays(10));
                return Task.FromResult(response);
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(config, FakeKey, longBackoff))
                await ExpectCode("timeout", () => client.CompleteAsync(Messages(), new JArray(), CancellationToken.None)).ConfigureAwait(false);
            Check(longBackoff.Calls == 1, "overall deadline covers backoff");
        }

        private static async Task EchoRoundTrip()
        {
            int round = 0;
            string nonce = null;
            FakeHandler echo = new FakeHandler(async (request, token) =>
            {
                JObject body = JObject.Parse(await request.Content.ReadAsStringAsync().ConfigureAwait(false));
                JArray tools = (JArray)body["tools"];
                Check(tools.Count == 1 && (string)tools[0]["function"]["name"] == "oni_connection_echo", "test exposes only harmless echo");
                if (++round == 1)
                {
                    nonce = (string)tools[0]["function"]["parameters"]["properties"]["text"]["enum"][0];
                    Check((string)body["tool_choice"]["function"]["name"] == "oni_connection_echo", "test forces echo tool");
                    return Json(ToolResponse("oni_connection_echo", nonce));
                }
                Check((string)body["tool_choice"] == "none", "followup prevents further tools");
                JArray messages = (JArray)body["messages"];
                Check(messages.Count == 3 && (string)messages[2]["role"] == "tool" && (string)messages[2]["tool_call_id"] == "echo-call", "matching tool result ID");
                Check((string)JObject.Parse((string)messages[2]["content"])["text"] == nonce, "local echo result");
                return Json(ResponseObject(nonce));
            });
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, echo))
            {
                ConnectionTestResult result = await client.TestConnectionAsync(CancellationToken.None).ConfigureAwait(false);
                Check(result.Success && echo.Calls == 2, "verified two-message echo round trip");
            }
        }

        private static async Task RejectWrongEchoTool()
        {
            FakeHandler wrong = new FakeHandler((request, token) => Task.FromResult(Json(ToolResponse("set_speed", "wrong"))));
            using (OpenAiCompatibleClient client = new OpenAiCompatibleClient(Config(), FakeKey, wrong))
                await ExpectCode("connection_test_missing_echo_call", () => client.TestConnectionAsync(CancellationToken.None)).ConfigureAwait(false);
            Check(wrong.Calls == 1, "test never invokes game tools");
        }

        private static void CredentialsFailClosed()
        {
            if (CredentialStore.IsSupported) return; // Do not save even a fake key on the developer machine.
            CredentialStore store = new CredentialStore(Path.Combine(Path.GetTempPath(), "oni-agent-harness-do-not-create"));
            try
            {
                store.Load();
                throw new Exception("Expected unsupported DPAPI to fail closed.");
            }
            catch (CredentialStoreException error)
            {
                Check(error.Code == "dpapi_unavailable" && error.InnerException == null, "unsupported credentials fail closed");
            }
        }

        private static JObject ToolResponse(string name, string text)
        {
            return new JObject
            {
                ["choices"] = new JArray(new JObject
                {
                    ["finish_reason"] = "tool_calls",
                    ["message"] = new JObject
                    {
                        ["role"] = "assistant",
                        ["content"] = JValue.CreateNull(),
                        ["tool_calls"] = new JArray(new JObject
                        {
                            ["id"] = "echo-call", ["type"] = "function",
                            ["function"] = new JObject { ["name"] = name, ["arguments"] = new JObject { ["text"] = text }.ToString(Formatting.None) }
                        })
                    }
                })
            };
        }

        private static JObject ResponseObject(string text)
        {
            return new JObject { ["choices"] = new JArray(new JObject { ["finish_reason"] = "stop", ["message"] = new JObject { ["role"] = "assistant", ["content"] = text } }) };
        }

        private static HttpResponseMessage Json(JObject body)
        {
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body.ToString(Formatting.None), Encoding.UTF8, "application/json") };
        }

        private static async Task<AgentModelException> ExpectCode(string code, Func<Task> action)
        {
            try { await action().ConfigureAwait(false); }
            catch (AgentModelException error)
            {
                Check(error.Code == code, "expected " + code + ", got " + error.Code);
                Check(!error.ToString().Contains(FakeKey), "errors do not contain credential");
                return error;
            }
            throw new Exception("Expected model failure: " + code);
        }

        private static void ExpectArgument(Action action)
        {
            try { action(); }
            catch (ArgumentException) { return; }
            throw new Exception("Expected invalid configuration to be rejected.");
        }

        private static void Check(bool condition, string description)
        {
            if (!condition) throw new Exception("Client harness assertion failed: " + description);
        }

        private sealed class FakeHandler : HttpMessageHandler
        {
            private readonly Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> respond;
            public int Calls { get; private set; }
            public FakeHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> respond) { this.respond = respond; }
            protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
            {
                Calls++;
                return respond(request, cancellationToken);
            }
        }

        private sealed class NonSeekableStream : Stream
        {
            private readonly MemoryStream inner;
            public NonSeekableStream(byte[] bytes) { inner = new MemoryStream(bytes); }
            public override bool CanRead => true;
            public override bool CanSeek => false;
            public override bool CanWrite => false;
            public override long Length => throw new NotSupportedException();
            public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
            public override int Read(byte[] buffer, int offset, int count) => inner.Read(buffer, offset, count);
            public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken token) => inner.ReadAsync(buffer, offset, count, token);
            public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
            public override void SetLength(long value) => throw new NotSupportedException();
            public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
            public override void Flush() { }
            protected override void Dispose(bool disposing) { if (disposing) inner.Dispose(); base.Dispose(disposing); }
        }
    }
}
