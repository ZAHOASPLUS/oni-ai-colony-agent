using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// A call waiting for the game's main thread.  A listener thread gives up on
    /// a call only while it is still pending, so a call that the main thread has
    /// claimed always delivers its response and an abandoned one never runs.
    /// </summary>
    internal sealed class ApiCall
    {
        private const int Pending = 0;
        private const int Claimed = 1;
        private const int Abandoned = 2;

        private readonly ManualResetEventSlim done = new ManualResetEventSlim(false);
        private int state = Pending;
        private JObject response;

        internal ApiCall(string tool, JObject args)
        {
            Tool = tool;
            Args = args;
        }

        internal string Tool { get; }

        internal JObject Args { get; }

        internal bool TryClaim()
        {
            return Interlocked.CompareExchange(ref state, Claimed, Pending) == Pending;
        }

        internal void Complete(JObject value)
        {
            response = value;
            done.Set();
        }

        internal JObject Await(int timeoutMilliseconds)
        {
            if (done.Wait(timeoutMilliseconds))
            {
                return response;
            }
            if (Interlocked.CompareExchange(ref state, Abandoned, Pending) == Pending)
            {
                return ApiResult.Failure(
                    "main_thread_timeout",
                    "The game's main thread did not take this call in time; it was not run.",
                    "The game is probably loading or saving. Check get_lifecycle, then call again.");
            }
            // Already running on the main thread: its outcome must not be lost.
            done.Wait();
            return response;
        }
    }

    internal static class ApiJson
    {
        // JObject.Parse turns date-like strings into local-time DateTime values,
        // which silently moved autoload's created_utc by the machine's UTC offset.
        internal static JObject Parse(string text)
        {
            using (JsonTextReader reader = new JsonTextReader(new StringReader(text)))
            {
                reader.DateParseHandling = DateParseHandling.None;
                return JObject.Load(reader);
            }
        }
    }

    internal static class ApiResult
    {
        internal static JObject Success(object result)
        {
            return new JObject
            {
                ["ok"] = true,
                ["result"] = result as JToken ?? (result == null ? JValue.CreateNull() : JToken.FromObject(result))
            };
        }

        internal static JObject Failure(string code, string message, string hint = null)
        {
            JObject error = new JObject
            {
                ["code"] = code,
                ["message"] = message
            };
            if (!string.IsNullOrEmpty(hint))
            {
                error["hint"] = hint;
            }
            return new JObject
            {
                ["ok"] = false,
                ["error"] = error
            };
        }
    }

    /// <summary>
    /// A minimal HTTP/1.1 server for <c>POST /call</c> on the loopback interface.
    /// One request per connection.  TcpListener rather than HttpListener so the
    /// whole path can be exercised outside the game's Mono (tools/ApiServerHarness).
    /// </summary>
    internal sealed class ApiServer
    {
        internal const string TokenHeader = "x-oni-token";
        private const int HeadLimitBytes = 16 * 1024;
        private const int BodyLimitBytes = 1024 * 1024;
        private const int SocketTimeoutMilliseconds = 5000;

        private readonly ConcurrentQueue<ApiCall> calls = new ConcurrentQueue<ApiCall>();
        private readonly byte[] tokenBytes;
        private readonly int mainThreadTimeoutMilliseconds;
        private TcpListener listener;
        private volatile bool running;

        internal ApiServer(string token, int mainThreadTimeoutMilliseconds)
        {
            Token = token;
            tokenBytes = Encoding.UTF8.GetBytes(token);
            this.mainThreadTimeoutMilliseconds = mainThreadTimeoutMilliseconds;
        }

        internal string Token { get; }

        internal int Port { get; private set; }

        internal static string NewToken()
        {
            byte[] bytes = new byte[32];
            using (RandomNumberGenerator random = RandomNumberGenerator.Create())
            {
                random.GetBytes(bytes);
            }
            return BitConverter.ToString(bytes).Replace("-", string.Empty).ToLowerInvariant();
        }

        internal void Start()
        {
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            running = true;
            Thread thread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "ONI AI API"
            };
            thread.Start();
        }

        internal void Stop()
        {
            running = false;
            try
            {
                listener?.Stop();
            }
            catch (SocketException)
            {
                // Already closed.
            }
        }

        /// <summary>Main thread only: the next call that was not abandoned.</summary>
        internal bool TryTake(out ApiCall call)
        {
            while (calls.TryDequeue(out call))
            {
                if (call.TryClaim())
                {
                    return true;
                }
            }
            return false;
        }

        private void AcceptLoop()
        {
            while (running)
            {
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch (Exception) when (!running)
                {
                    return;
                }
                catch (SocketException)
                {
                    continue;
                }
                ThreadPool.QueueUserWorkItem(_ => Serve(client));
            }
        }

        private void Serve(TcpClient client)
        {
            try
            {
                using (client)
                {
                    client.ReceiveTimeout = SocketTimeoutMilliseconds;
                    client.SendTimeout = SocketTimeoutMilliseconds;
                    using (NetworkStream stream = client.GetStream())
                    {
                        int status;
                        JObject response = Handle(stream, out status);
                        WriteResponse(stream, status, response);
                    }
                }
            }
            catch (IOException)
            {
                // The caller went away; there is nobody left to answer.
            }
            catch (SocketException)
            {
                // As above.
            }
            catch (ObjectDisposedException)
            {
                // Shutting down.
            }
        }

        private JObject Handle(Stream stream, out int status)
        {
            string requestLine;
            Dictionary<string, string> headers;
            byte[] overflow;
            if (!TryReadHead(stream, out requestLine, out headers, out overflow))
            {
                status = 400;
                return ApiResult.Failure("bad_request", "The HTTP request head is malformed or too large.");
            }

            string[] parts = requestLine.Split(' ');
            if (parts.Length != 3 || parts[0] != "POST" || parts[1] != "/call")
            {
                status = 404;
                return ApiResult.Failure("not_found", "Only POST /call is served.");
            }

            string supplied;
            headers.TryGetValue(TokenHeader, out supplied);
            if (!TokenMatches(supplied))
            {
                status = 401;
                return ApiResult.Failure("unauthorized", "The " + TokenHeader + " header is missing or wrong.");
            }

            string lengthText;
            int length;
            if (!headers.TryGetValue("content-length", out lengthText) ||
                !int.TryParse(lengthText, out length) ||
                length < 0 ||
                length > BodyLimitBytes)
            {
                status = 400;
                return ApiResult.Failure("bad_request", "Content-Length is missing, invalid or above the limit.");
            }

            byte[] body = new byte[length];
            if (!TryReadBody(stream, overflow, body))
            {
                status = 400;
                return ApiResult.Failure("bad_request", "The request body ended early.");
            }

            string tool;
            JObject args;
            if (!TryParseCall(body, out tool, out args))
            {
                status = 400;
                return ApiResult.Failure(
                    "bad_request",
                    "The body must be JSON: {\"tool\": \"<name>\", \"args\": {...}}.");
            }

            status = 200;
            ApiCall call = new ApiCall(tool, args);
            calls.Enqueue(call);
            return call.Await(mainThreadTimeoutMilliseconds);
        }

        private bool TokenMatches(string supplied)
        {
            byte[] candidate = Encoding.UTF8.GetBytes(supplied ?? string.Empty);
            if (candidate.Length != tokenBytes.Length)
            {
                return false;
            }
            int difference = 0;
            for (int i = 0; i < tokenBytes.Length; i++)
            {
                difference |= candidate[i] ^ tokenBytes[i];
            }
            return difference == 0;
        }

        private static bool TryParseCall(byte[] body, out string tool, out JObject args)
        {
            tool = null;
            args = null;
            JObject request;
            try
            {
                request = ApiJson.Parse(Encoding.UTF8.GetString(body));
            }
            catch (JsonException)
            {
                return false;
            }

            tool = (request["tool"] as JValue)?.Value as string;
            JToken rawArgs = request["args"];
            if (string.IsNullOrEmpty(tool) ||
                (rawArgs != null && rawArgs.Type != JTokenType.Object && rawArgs.Type != JTokenType.Null))
            {
                return false;
            }
            args = rawArgs as JObject ?? new JObject();
            return true;
        }

        private static bool TryReadHead(
            Stream stream,
            out string requestLine,
            out Dictionary<string, string> headers,
            out byte[] overflow)
        {
            requestLine = null;
            headers = null;
            overflow = null;
            byte[] buffer = new byte[HeadLimitBytes];
            int filled = 0;
            int headEnd = -1;
            while (headEnd < 0)
            {
                if (filled == buffer.Length)
                {
                    return false;
                }
                int read = stream.Read(buffer, filled, buffer.Length - filled);
                if (read <= 0)
                {
                    return false;
                }
                int searchFrom = Math.Max(0, filled - 3);
                filled += read;
                for (int i = searchFrom; i + 3 < filled; i++)
                {
                    if (buffer[i] == '\r' && buffer[i + 1] == '\n' && buffer[i + 2] == '\r' && buffer[i + 3] == '\n')
                    {
                        headEnd = i;
                        break;
                    }
                }
            }

            string[] lines = Encoding.ASCII.GetString(buffer, 0, headEnd).Split(new[] { "\r\n" }, StringSplitOptions.None);
            requestLine = lines[0];
            headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < lines.Length; i++)
            {
                int colon = lines[i].IndexOf(':');
                if (colon <= 0)
                {
                    return false;
                }
                headers[lines[i].Substring(0, colon).Trim()] = lines[i].Substring(colon + 1).Trim();
            }

            int bodyStart = headEnd + 4;
            overflow = new byte[filled - bodyStart];
            Array.Copy(buffer, bodyStart, overflow, 0, overflow.Length);
            return true;
        }

        private static bool TryReadBody(Stream stream, byte[] overflow, byte[] body)
        {
            int filled = Math.Min(overflow.Length, body.Length);
            Array.Copy(overflow, body, filled);
            while (filled < body.Length)
            {
                int read = stream.Read(body, filled, body.Length - filled);
                if (read <= 0)
                {
                    return false;
                }
                filled += read;
            }
            return true;
        }

        private static void WriteResponse(Stream stream, int status, JObject response)
        {
            byte[] body = Encoding.UTF8.GetBytes(response.ToString(Formatting.None));
            string head =
                "HTTP/1.1 " + status + " " + ReasonPhrase(status) + "\r\n" +
                "Content-Type: application/json; charset=utf-8\r\n" +
                "Content-Length: " + body.Length + "\r\n" +
                "Connection: close\r\n\r\n";
            byte[] headBytes = Encoding.ASCII.GetBytes(head);
            stream.Write(headBytes, 0, headBytes.Length);
            stream.Write(body, 0, body.Length);
            stream.Flush();
        }

        private static string ReasonPhrase(int status)
        {
            switch (status)
            {
                case 200:
                    return "OK";
                case 401:
                    return "Unauthorized";
                case 404:
                    return "Not Found";
                default:
                    return "Bad Request";
            }
        }
    }
}
