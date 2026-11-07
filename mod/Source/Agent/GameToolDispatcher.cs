using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;

namespace OniAiProbeMod.Agent
{
    /// <summary>Workers queue immutable JSON copies; only Pump touches the game.</summary>
    internal sealed class GameToolDispatcher : IAgentToolDispatcher, IDisposable
    {
        private sealed class Pending
        {
            internal string Session, Tool, Call;
            internal long Generation;
            internal JObject Arguments;
            internal CancellationToken Token;
            internal CancellationTokenRegistration Registration;
            internal readonly TaskCompletionSource<JObject> Result =
                new TaskCompletionSource<JObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        private readonly ConcurrentQueue<Pending> queue = new ConcurrentQueue<Pending>();
        private readonly Func<string, long, bool> canDispatch;
        private readonly Func<string, JObject, JObject> beforeDispatch;
        private int count;
        private volatile bool disposed;

        internal GameToolDispatcher(Func<string, long, bool> canDispatch,
            Func<string, JObject, JObject> beforeDispatch = null)
        {
            this.canDispatch = canDispatch ?? throw new ArgumentNullException(nameof(canDispatch));
            this.beforeDispatch = beforeDispatch;
        }

        public Task<JObject> DispatchAsync(string sessionId, long generation, string toolCallId,
            string toolName, JObject arguments, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try { ApiToolSchema.Validate(toolName, arguments); }
            catch (ApiException ex) { return Task.FromResult(ApiTools.Refusal(ex)); }
            if (toolName == "wait") return WaitAsync(sessionId, generation, toolCallId, arguments, cancellationToken);
            // load_save takes paths and is not an autosave capability. Never give
            // an in-mod model a route around the yet-unverified native L3 adapter.
            if (toolName == "load_save")
                return Task.FromResult(ApiResult.Failure("rollback_unavailable", "Native autosave identity/loading is not verified; path-based loads are unavailable to the Agent."));
            if (disposed) return Task.FromResult(ApiResult.Failure("dispatcher_stopped", "Dispatcher is stopped."));
            if (Interlocked.Increment(ref count) > 32)
            {
                Interlocked.Decrement(ref count);
                return Task.FromResult(ApiResult.Failure("queue_full", "Main-thread queue is full."));
            }
            Pending pending = new Pending
            {
                Session = sessionId, Generation = generation, Call = toolCallId, Tool = toolName,
                Arguments = (JObject)arguments.DeepClone(), Token = cancellationToken
            };
            pending.Registration = cancellationToken.Register(() => pending.Result.TrySetCanceled());
            queue.Enqueue(pending);
            // Dispose may race enqueue; cancelled results cannot execute in Pump.
            if (disposed) pending.Result.TrySetCanceled();
            return pending.Result.Task;
        }

        /// <summary>Unity Update only. Check current session/generation at the last possible point.</summary>
        internal void Pump()
        {
            for (int i = 0; i < 8 && queue.TryDequeue(out Pending pending); i++)
            {
                Interlocked.Decrement(ref count);
                using (pending.Registration)
                {
                    if (disposed || pending.Token.IsCancellationRequested || pending.Result.Task.IsCompleted ||
                        !canDispatch(pending.Session, pending.Generation))
                    {
                        pending.Result.TrySetCanceled();
                        continue;
                    }
                    JObject result;
                    Stopwatch elapsed = Stopwatch.StartNew();
                    try
                    {
                        result = beforeDispatch?.Invoke(pending.Tool, pending.Arguments);
                        if (result == null)
                            result = ApiTools.Invoke(pending.Tool, pending.Arguments, "agent", pending.Session);
                    }
                    catch (Exception)
                    {
                        result = ApiResult.Failure("dispatch_failed", "The game handler failed. Inspect the game log.");
                    }
                    result["_agent_dispatch"] = new JObject
                    {
                        ["execution_ms"] = elapsed.Elapsed.TotalMilliseconds,
                        ["game_cycle"] = GameClock.Instance == null ? JValue.CreateNull() :
                            new JValue(Math.Round(GameClock.Instance.GetTimeInCycles() + 1.0, 3))
                    };
                    pending.Result.TrySetResult(result);
                }
            }
        }

        private async Task<JObject> WaitAsync(string session, long generation, string call,
            JObject arguments, CancellationToken token)
        {
            double seconds = Math.Min(60, Math.Max(0, (double?)arguments["seconds"] ?? 10));
            long since = (long?)arguments["since"] ?? 0;
            long missed = 0;
            bool backlog = true, truncated = false;
            JArray events = new JArray();
            Stopwatch timer = Stopwatch.StartNew();
            do
            {
                token.ThrowIfCancellationRequested();
                JObject outcome = await DispatchAsync(session, generation, call, "get_events",
                    new JObject { ["since"] = since, ["limit"] = Math.Min(100, 512 - events.Count) }, token).ConfigureAwait(false);
                if ((bool?)outcome["ok"] != true) return outcome;
                JObject result = (JObject)outcome["result"];
                JArray arrived = result["events"] as JArray ?? new JArray();
                bool attention = false;
                foreach (JToken item in arrived)
                {
                    if (events.Count < 512) events.Add(item.DeepClone()); else truncated = true;
                    string type = (string)item["type"], severity = (string)item["severity"];
                    attention |= type == "duplicant_lost" || type == "duplicant_low_breath" ||
                        (type == "alert_raised" && (severity == "critical" || severity == "bad"));
                }
                missed += (long?)result["missed"] ?? 0;
                since = (long?)result["next_since"] ?? since;
                // Stop at the last retained event. Never advance a cursor past
                // events dropped to enforce this wait's memory budget.
                if (events.Count >= 512) { truncated = true; break; }
                double remaining = seconds - timer.Elapsed.TotalSeconds;
                if (remaining <= 0 || (!backlog && attention) || truncated) break;
                if ((bool?)result["more"] == true) continue;
                backlog = false;
                await Task.Delay(TimeSpan.FromSeconds(Math.Min(2, remaining)), token).ConfigureAwait(false);
            } while (true);
            return ApiResult.Success(new JObject
            {
                ["waited_seconds"] = Math.Round(timer.Elapsed.TotalSeconds, 1),
                ["events"] = events, ["next_since"] = since, ["missed"] = missed,
                ["truncated"] = truncated
            });
        }

        public void Dispose()
        {
            disposed = true;
            while (queue.TryDequeue(out Pending pending))
            {
                Interlocked.Decrement(ref count);
                pending.Result.TrySetCanceled();
                pending.Registration.Dispose();
            }
        }
    }
}
