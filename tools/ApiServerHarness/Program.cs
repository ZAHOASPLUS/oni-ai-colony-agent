using System;
using System.Collections.Generic;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;

namespace ApiServerHarness
{
    // Drives the Mod's HTTP server over real loopback sockets, with this thread
    // standing in for the game's main thread.
    internal static class Program
    {
        private const string Token = "harness-token";
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            ApiServer server = new ApiServer(Token, 300);
            server.Start();
            try
            {
                Run(server);
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.api-server-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(Passed.ToArray())
                }.ToString(Newtonsoft.Json.Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex);
                return 1;
            }
            finally
            {
                server.Stop();
            }
        }

        private static void Run(ApiServer server)
        {
            Check("rejects_wrong_path", Raw(server, "GET /call HTTP/1.1\r\n\r\n").StartsWith("HTTP/1.1 404", StringComparison.Ordinal));
            Check("rejects_missing_token", Raw(server, Request("{\"tool\":\"x\"}", null)).StartsWith("HTTP/1.1 401", StringComparison.Ordinal));
            Check("rejects_wrong_token", Raw(server, Request("{\"tool\":\"x\"}", "nope")).StartsWith("HTTP/1.1 401", StringComparison.Ordinal));
            Check("rejects_bad_json", Raw(server, Request("{not json", Token)).StartsWith("HTTP/1.1 400", StringComparison.Ordinal));
            Check("rejects_non_object_args", Raw(server, Request("{\"tool\":\"x\",\"args\":[1]}", Token)).StartsWith("HTTP/1.1 400", StringComparison.Ordinal));

            // Answered by the "main thread", with a non-ASCII payload.
            string answered = null;
            Thread caller = Call(() => answered = Raw(server, Request("{\"tool\":\"echo\",\"args\":{\"name\":\"氧气\"}}", Token)));
            ApiCall call = TakeWithin(server, 2000);
            Check("delivers_tool_and_args", call != null && call.Tool == "echo" && (string)call.Args["name"] == "氧气");
            call.Complete(ApiResult.Success(new JObject { ["name"] = call.Args["name"] }));
            caller.Join();
            JObject body = JObject.Parse(answered.Substring(answered.IndexOf("\r\n\r\n", StringComparison.Ordinal) + 4));
            Check("returns_utf8_result", (bool)body["ok"] && (string)body["result"]["name"] == "氧气");

            // A date-like string stays the exact string the caller sent.
            const string Stamp = "2026-10-30T17:59:03.872334+00:00";
            caller = Call(() => answered = Raw(server, Request("{\"tool\":\"stamp\",\"args\":{\"at\":\"" + Stamp + "\"}}", Token)));
            call = TakeWithin(server, 2000);
            JToken stamp = call.Args["at"];
            Check("keeps_date_strings", stamp.Type == JTokenType.String && (string)stamp == Stamp);
            call.Complete(ApiResult.Success(new JValue("ok")));
            caller.Join();

            // Arguments default to an empty object.
            caller = Call(() => answered = Raw(server, Request("{\"tool\":\"bare\"}", Token)));
            call = TakeWithin(server, 2000);
            Check("defaults_args", call != null && call.Args.Count == 0);
            call.Complete(ApiResult.Failure("nope", "refused", "try again"));
            caller.Join();
            body = JObject.Parse(answered.Substring(answered.IndexOf("\r\n\r\n", StringComparison.Ordinal) + 4));
            Check("returns_error_shape", !(bool)body["ok"] && (string)body["error"]["hint"] == "try again");

            // Nobody pumps: the listener gives up and the call must never run.
            answered = Raw(server, Request("{\"tool\":\"late\"}", Token));
            Check("times_out_unclaimed", answered.Contains("main_thread_timeout"));
            ApiCall abandoned;
            Check("never_runs_abandoned", !server.TryTake(out abandoned));

            // Claimed before the timeout: the caller waits for the real answer.
            caller = Call(() => answered = Raw(server, Request("{\"tool\":\"slow\"}", Token)));
            call = TakeWithin(server, 2000);
            Thread.Sleep(500);
            call.Complete(ApiResult.Success(new JValue("finished")));
            caller.Join();
            Check("keeps_claimed_result", answered.Contains("finished"));
        }

        // Background, so a failed case cannot keep the process alive.
        private static Thread Call(ThreadStart body)
        {
            Thread thread = new Thread(body) { IsBackground = true };
            thread.Start();
            return thread;
        }

        private static ApiCall TakeWithin(ApiServer server, int milliseconds)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(milliseconds);
            while (DateTime.UtcNow < deadline)
            {
                ApiCall call;
                if (server.TryTake(out call))
                {
                    return call;
                }
                Thread.Sleep(5);
            }
            return null;
        }

        private static string Request(string body, string token)
        {
            string tokenHeader = token == null ? string.Empty : ApiServer.TokenHeader + ": " + token + "\r\n";
            return "POST /call HTTP/1.1\r\nHost: 127.0.0.1\r\n" + tokenHeader +
                "Content-Length: " + Encoding.UTF8.GetByteCount(body) + "\r\n\r\n" + body;
        }

        private static string Raw(ApiServer server, string request)
        {
            using (TcpClient client = new TcpClient("127.0.0.1", server.Port))
            using (NetworkStream stream = client.GetStream())
            {
                byte[] bytes = Encoding.UTF8.GetBytes(request);
                // Split the write so the head and body arrive separately.
                int half = bytes.Length / 2;
                stream.Write(bytes, 0, half);
                stream.Flush();
                Thread.Sleep(5);
                stream.Write(bytes, half, bytes.Length - half);
                byte[] buffer = new byte[65536];
                StringBuilder response = new StringBuilder();
                int read;
                while ((read = stream.Read(buffer, 0, buffer.Length)) > 0)
                {
                    response.Append(Encoding.UTF8.GetString(buffer, 0, read));
                }
                return response.ToString();
            }
        }

        private static void Check(string name, bool condition)
        {
            if (!condition)
            {
                throw new InvalidOperationException("Case failed: " + name);
            }
            Passed.Add(name);
        }
    }
}
