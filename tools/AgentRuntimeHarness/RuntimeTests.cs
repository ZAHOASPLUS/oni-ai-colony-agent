using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent.Harness
{
    internal static class RuntimeTests
    {
        internal static async Task RunAll()
        {
            ContextIsBoundedAndAtomic();
            WriteLeaseHasOneOwner();
            await SerialCallsProduceCompleteHistory();
            await CorrectionDiscardsLateModelResponse();
            await CorrectionDropsRemainingToolCalls();
            await QueuedDispatchRevalidatesGeneration();
            await PauseResumeCancelsAndRefreshes();
            await StopInvalidatesLateResponse();
            await SessionChangeRejectsOldGeneration();
            await PerToolErrorsRemainPaired();
            await OversizedBatchExecutesNothing();
            await BudgetsAreEnforced();
            await SessionTimeBudgetCancelsRequest();
            await FailedStateRefreshPreventsPlanning();
            await ModelErrorsRetainSafeDiagnostics();
            Console.WriteLine("PASS: runtime/context/lease tests (15 cases).");
        }

        private static void ContextIsBoundedAndAtomic()
        {
            var context = new AgentContext("system", "goal", "knowledge", 6, 4000, 1000);
            for (int turn = 0; turn < 8; turn++)
            {
                context.AppendTurn(Calls("a" + turn, "b" + turn), new[]
                {
                    new JObject { ["value"] = new string('"', 10000) },
                    new JObject { ["value"] = new string('x', 10000) }
                });
                JArray snapshot = context.Snapshot();
                Assert(snapshot.Count <= 6, "Context message limit exceeded.");
                Assert(snapshot.ToString(Formatting.None).Length <= 4000, "Context character limit exceeded.");
                AssertProtocol(snapshot);
                var results = snapshot.OfType<JObject>().Where(m => (string)m["role"] == "tool").ToArray();
                Assert(results.Length == 2, "A complete latest tool exchange must remain.");
                Assert(results.All(m => (bool)JObject.Parse((string)m["content"])["truncated"]), "Truncation must be explicit.");
            }
            string before = context.Snapshot().ToString(Formatting.None);
            Expect<ArgumentException>(() => context.AppendTurn(Calls("missing"), new JObject[0]));
            Assert(before == context.Snapshot().ToString(Formatting.None), "A partial group changed context.");
            context.Correct("Preserve the water reservoir.");
            AssertProtocol(context.Snapshot());
            Assert(context.Snapshot().ToString().Contains("Preserve the water reservoir."), "Latest correction was lost.");
            context.Correct("The reservoir restriction is replaced; prioritize oxygen now.");
            JArray corrected = context.Snapshot();
            Assert((string)corrected.Last["role"] == "user" &&
                ((string)corrected.Last["content"]).Contains("prioritize oxygen now."),
                "A superseded instruction appeared after the latest correction.");
            AssertProtocol(corrected);
            JArray detached = context.Snapshot();
            ((JObject)detached[0])["content"] = "tampered";
            Assert((string)context.Snapshot()[0]["content"] == "system\n\nTask knowledge:\nknowledge", "Snapshot mutated source history.");
        }

        private static void WriteLeaseHasOneOwner()
        {
            Assert(AgentWriteLease.Allows("external"), "Initial external access should be available.");
            Assert(AgentWriteLease.Acquire("one"), "First lease failed.");
            try
            {
                Assert(!AgentWriteLease.Acquire("two"), "Two sessions acquired write ownership.");
                Assert(AgentWriteLease.Allows("agent", "one"), "Owner was rejected.");
                Assert(!AgentWriteLease.Allows("agent", "two"), "Wrong agent session was accepted.");
                Assert(!AgentWriteLease.Allows("external"), "External writes were allowed during agent lease.");
                AgentWriteLease.Release("two");
                Assert(!AgentWriteLease.Allows("external"), "Wrong owner released lease.");
            }
            finally { AgentWriteLease.Release("one"); }
            Assert(AgentWriteLease.Allows("external"), "Lease was not released.");
        }

        private static async Task SerialCallsProduceCompleteHistory()
        {
            var model = new Model((n, messages, token) => Task.FromResult(n == 1 ? Calls("a", "b") : Final()));
            int active = 0;
            int maximum = 0;
            var dispatch = new Dispatcher(async (name, token) =>
            {
                int count = Interlocked.Increment(ref active);
                maximum = Math.Max(maximum, count);
                await Task.Delay(5, token);
                Interlocked.Decrement(ref active);
                return Success(name);
            });
            using (var runtime = New(model, dispatch))
            {
                // Faulty UI subscribers must not break dispatch or generation control.
                runtime.EventRaised += ignored => throw new InvalidOperationException("UI failure");
                runtime.Start("colony", "Keep the colony alive.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 2, "Expected one tool turn and one final answer.");
                Assert(maximum == 1 && model.MaxConcurrent == 1, "Execution overlapped.");
                Assert(dispatch.Names.SequenceEqual(new[] { "get_status", "a", "b" }), "Tool order changed.");
                JArray second = model.Requests.ToArray()[1];
                AssertProtocol(second);
                Assert(second.OfType<JObject>().Count(m => (string)m["role"] == "tool") == 3, "Result pairs are missing.");
            }
        }

        private static async Task CorrectionDiscardsLateModelResponse()
        {
            var delayed = NewSignal<JObject>();
            var model = new Model((n, messages, token) => n == 1 ? delayed.Task : Task.FromResult(Final()));
            var dispatch = new Dispatcher();
            var events = new ConcurrentQueue<AgentRuntimeEvent>();
            using (var runtime = New(model, dispatch))
            {
                runtime.EventRaised += events.Enqueue;
                runtime.Start("colony", "Original goal.");
                await Until(() => model.Count == 1);
                long old = runtime.Generation;
                runtime.Correct("Do not dig the reservoir.");
                Assert(!runtime.CanDispatch("colony", old), "Correction left the old generation dispatchable.");
                delayed.SetResult(Calls("a")); // Deliberately non-cooperative client.
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 2 && model.MaxConcurrent == 1, "Correction created overlapping model calls.");
                Assert(dispatch.Names.All(name => name == "get_status"), "A stale model response executed actions.");
                Assert(dispatch.Names.Count == 2, "Correction did not refresh state.");
                Assert(events.Any(e => e.Kind == "stale_response_discarded"), "Stale response was not recorded.");
                Assert(model.Requests.Last().ToString().Contains("Do not dig the reservoir."), "New instruction was not sent.");
                AssertProtocol(model.Requests.Last());
            }
        }

        private static async Task CorrectionDropsRemainingToolCalls()
        {
            var started = NewSignal<bool>();
            var release = NewSignal<JObject>();
            var model = new Model((n, messages, token) => Task.FromResult(n == 1 ? Calls("a", "b") : Final()));
            var dispatch = new Dispatcher((name, token) =>
            {
                if (name != "a") return Task.FromResult(Success(name));
                started.TrySetResult(true);
                return release.Task; // A handler already executing is allowed to finish safely.
            });
            var events = new ConcurrentQueue<AgentRuntimeEvent>();
            using (var runtime = New(model, dispatch))
            {
                runtime.EventRaised += events.Enqueue;
                runtime.Start("colony", "Original goal.");
                await Deadline(started.Task);
                runtime.Correct("Now prioritize oxygen.");
                release.SetResult(Success("a"));
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(!dispatch.Names.Contains("b"), "An unexecuted old tool was replayed.");
                Assert(events.Any(e => e.Kind == "tool_completed_stale" && e.ToolName == "a"), "Completed interrupted handler lost its audit event.");
                JArray latest = model.Requests.Last();
                AssertProtocol(latest);
                Assert(!latest.OfType<JObject>().Any(m => (string)m["tool_call_id"] == "call_a"), "Partial old tool exchange leaked into new context.");
            }
        }

        private static async Task PauseResumeCancelsAndRefreshes()
        {
            var cancelled = NewSignal<bool>();
            var model = new Model(async (n, messages, token) =>
            {
                if (n == 1)
                {
                    try { await Task.Delay(Timeout.Infinite, token); }
                    finally { cancelled.TrySetResult(true); }
                }
                return Final();
            });
            var dispatch = new Dispatcher();
            using (var runtime = New(model, dispatch))
            {
                runtime.Start("colony", "A task.");
                await Until(() => model.Count == 1);
                long generation = runtime.Generation;
                runtime.Pause();
                Assert(runtime.State == AgentRuntimeState.Paused && !runtime.CanDispatch("colony", generation), "Pause failed to invalidate work.");
                await Deadline(cancelled.Task);
                Assert(!dispatch.Names.Contains("set_speed"), "Pause Agent changed game speed.");
                runtime.Resume();
                await Until(() => runtime.State == AgentRuntimeState.Paused && model.Count == 2);
                Assert(dispatch.Names.Count == 2, "Resume failed to refresh game state.");
            }
        }

        private static async Task QueuedDispatchRevalidatesGeneration()
        {
            var model = new Model((n, messages, token) => Task.FromResult(n == 1 ? Calls("a", "b") : Final()));
            var dispatcher = new QueuedDispatcher();
            using (var runtime = new AgentRuntime(model, dispatcher, Schema(), Options()))
            {
                dispatcher.Allows = runtime.CanDispatch;
                runtime.Start("colony", "A task.");
                await Until(() => dispatcher.Pending.Count == 1);
                dispatcher.DrainOne(); // Initial fresh-state read.
                await Until(() => dispatcher.Pending.Count == 1);
                runtime.Correct("Change priorities before the queued action executes.");
                // Correct already invalidated generation even if its asynchronous token
                // cancellation callback has not run yet. The final main-thread gate is decisive.
                dispatcher.DrainOne();
                await Until(() => dispatcher.Pending.Count == 1);
                dispatcher.DrainOne(); // Fresh-state read for the replacement generation.
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(dispatcher.Executed.SequenceEqual(new[] { "get_status", "get_status" }),
                    "Queued stale work passed final generation validation.");
                Assert(model.MaxConcurrent == 1, "Queued cancellation created overlapping model work.");
            }
        }

        private static async Task StopInvalidatesLateResponse()
        {
            var delayed = NewSignal<JObject>();
            var model = new Model((n, messages, token) => delayed.Task);
            var dispatch = new Dispatcher();
            var events = new ConcurrentQueue<AgentRuntimeEvent>();
            var runtime = New(model, dispatch);
            runtime.EventRaised += events.Enqueue;
            runtime.Start("colony", "A task.");
            await Until(() => model.Count == 1);
            long old = runtime.Generation;
            runtime.Stop();
            Assert(runtime.State == AgentRuntimeState.Stopped && !runtime.CanDispatch("colony", old), "Stop left dispatch enabled.");
            delayed.SetResult(Calls("a"));
            await Until(() => events.Any(e => e.Kind == "stale_response_discarded"));
            Assert(!dispatch.Names.Contains("a"), "Stopped request executed a late action.");
            runtime.Dispose();
            await Deadline(runtime.Completion);
        }

        private static async Task SessionChangeRejectsOldGeneration()
        {
            var delayed = NewSignal<JObject>();
            var model = new Model((n, messages, token) => n == 1 ? delayed.Task : Task.FromResult(Final()));
            var dispatch = new Dispatcher();
            using (var runtime = New(model, dispatch))
            {
                runtime.Start("old_colony", "Old goal.");
                await Until(() => model.Count == 1);
                long old = runtime.Generation;
                runtime.Start("new_colony", "New goal.");
                Assert(!runtime.CanDispatch("old_colony", old), "Old session survived Start.");
                delayed.SetResult(Calls("a"));
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(runtime.SessionId == "new_colony", "Wrong active session.");
                Assert(dispatch.Names.All(name => name == "get_status"), "Old session executed an action.");
                Assert(!model.Requests.Last().ToString().Contains("Old goal."), "Old colony context survived new Start.");
            }
        }

        private static async Task PerToolErrorsRemainPaired()
        {
            JObject calls = Calls("a", "unknown", "b");
            calls["tool_calls"][0]["function"]["arguments"] = "not JSON";
            var model = new Model((n, messages, token) => Task.FromResult(n == 1 ? calls : Final()));
            var dispatch = new Dispatcher((name, token) => name == "b"
                ? throw new InvalidOperationException("Do not echo credentials or provider text.")
                : Task.FromResult(Success(name)));
            using (var runtime = New(model, dispatch))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                JArray messages = model.Requests.Last();
                AssertProtocol(messages);
                Assert(messages.ToString().Contains("invalid_arguments") && messages.ToString().Contains("unknown_tool") &&
                    messages.ToString().Contains("tool_execution_failed"), "Per-tool error results were lost.");
                Assert(!messages.ToString().Contains("echo credentials"), "Raw exception text leaked.");
                Assert(dispatch.Names.SequenceEqual(new[] { "get_status", "b" }), "Invalid calls reached dispatcher.");
            }
        }

        private static async Task OversizedBatchExecutesNothing()
        {
            var model = new Model((n, messages, token) => Task.FromResult(Calls("a", "b")));
            var dispatch = new Dispatcher();
            var options = Options();
            options.MaxToolCallsPerTurn = 1;
            using (var runtime = New(model, dispatch, options))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Faulted);
                Assert(dispatch.Names.SequenceEqual(new[] { "get_status" }), "Part of an oversized batch executed.");
            }
        }

        private static async Task BudgetsAreEnforced()
        {
            var model = new Model((n, messages, token) => Task.FromResult(Calls("a", "b")));
            var dispatch = new Dispatcher();
            var options = Options();
            options.MaxToolCallsTotal = 2; // Includes the mandatory fresh observation.
            using (var runtime = New(model, dispatch, options))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(dispatch.Names.SequenceEqual(new[] { "get_status", "a" }), "Tool budget exceeded.");
                Assert(model.Count == 1, "Additional model request occurred after budget exhaustion.");
                runtime.Correct("Another instruction must not reset the budget.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 1 && dispatch.Names.Count == 2, "Correction reset task budgets.");
            }
            options = Options();
            options.MaxModelTurns = 1;
            model = new Model((n, messages, token) => Task.FromResult(Calls("a")));
            using (var runtime = New(model, new Dispatcher(), options))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 1, "Model round-trip budget exceeded.");
            }
        }

        private static async Task SessionTimeBudgetCancelsRequest()
        {
            var model = new Model(async (n, messages, token) =>
            {
                await Task.Delay(Timeout.Infinite, token);
                return Final();
            });
            var options = Options();
            options.MaxSessionDuration = TimeSpan.FromMilliseconds(200);
            using (var runtime = New(model, new Dispatcher(), options))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 1, "Time budget test did not reach the model request.");
            }
        }

        private static async Task FailedStateRefreshPreventsPlanning()
        {
            var model = new Model((n, messages, token) => Task.FromResult(Calls("a")));
            var dispatch = new Dispatcher((name, token) => Task.FromResult(new JObject
                { ["error"] = new JObject { ["code"] = "game_not_loaded" } }));
            using (var runtime = New(model, dispatch))
            {
                runtime.Start("colony", "A task.");
                await Until(() => runtime.State == AgentRuntimeState.Paused);
                Assert(model.Count == 0, "Model planned without a successful fresh state read.");
            }
        }

        private static async Task ModelErrorsRetainSafeDiagnostics()
        {
            var model = new Model((n, messages, token) => throw new AgentModelException("http_error", 401));
            var events = new ConcurrentQueue<AgentRuntimeEvent>();
            using (var runtime = New(model, new Dispatcher()))
            {
                runtime.EventRaised += events.Enqueue;
                runtime.Start("colony", "A task.");
                await Until(() => events.Any(e => e.Kind == "model_error"));
                AgentRuntimeEvent failure = events.First(e => e.Kind == "model_error");
                Assert(runtime.State == AgentRuntimeState.Faulted && failure.Message.Contains("http_error") &&
                    failure.Message.Contains("401"), "Safe provider error code/status was lost.");
            }
        }

        private static AgentRuntime New(Model model, Dispatcher dispatch, AgentRuntimeOptions options = null)
        {
            return new AgentRuntime(model, dispatch, Schema(), options ?? Options());
        }
        private static JArray Schema()
        {
            return new JArray(new[] { "get_status", "a", "b" }.Select(name =>
                new JObject { ["type"] = "function", ["function"] = new JObject
                    { ["name"] = name, ["parameters"] = new JObject { ["type"] = "object" } } }));
        }
        private static AgentRuntimeOptions Options()
        { return new AgentRuntimeOptions { RefreshToolName = "get_status", ModelTurnDelay = TimeSpan.Zero }; }
        private static JObject Calls(params string[] names)
        {
            return new JObject { ["role"] = "assistant", ["content"] = "Public decision summary.",
                ["tool_calls"] = new JArray(names.Select(name => new JObject { ["id"] = "call_" + name,
                    ["type"] = "function", ["function"] = new JObject { ["name"] = name, ["arguments"] = "{}" } })) };
        }
        private static JObject Final() { return new JObject { ["role"] = "assistant", ["content"] = "Model reports done, not independently verified." }; }
        private static JObject Success(string name) { return new JObject { ["ok"] = true, ["data"] = new JObject { ["tool"] = name } }; }
        private static TaskCompletionSource<T> NewSignal<T>() { return new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously); }
        private static void Assert(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        private static void Expect<T>(Action action) where T : Exception
        { try { action(); } catch (T) { return; } throw new InvalidOperationException("Expected " + typeof(T).Name); }
        private static async Task Until(Func<bool> predicate)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(5);
            while (!predicate())
            {
                if (DateTime.UtcNow >= deadline) throw new TimeoutException("Runtime test condition was not reached.");
                await Task.Delay(5);
            }
        }
        private static async Task Deadline(Task task)
        {
            if (await Task.WhenAny(task, Task.Delay(5000)) != task) throw new TimeoutException("Runtime test task timed out.");
            await task;
        }
        private static void AssertProtocol(JArray messages)
        {
            var pending = new HashSet<string>(StringComparer.Ordinal);
            foreach (JObject message in messages)
            {
                string role = (string)message["role"];
                if (role == "tool")
                {
                    Assert(pending.Remove((string)message["tool_call_id"]), "Orphaned or duplicate tool result.");
                    continue;
                }
                Assert(pending.Count == 0, "Assistant/user message split a tool exchange.");
                if (message["tool_calls"] is JArray calls)
                    foreach (JToken call in calls) Assert(pending.Add((string)call["id"]), "Duplicate tool-call id.");
            }
            Assert(pending.Count == 0, "Incomplete tool exchange at request end.");
        }

        private sealed class Model : IAgentModelClient
        {
            private readonly Func<int, JArray, CancellationToken, Task<JObject>> handler;
            private int count;
            private int active;
            internal readonly ConcurrentQueue<JArray> Requests = new ConcurrentQueue<JArray>();
            internal int Count { get { return Volatile.Read(ref count); } }
            internal int MaxConcurrent;
            internal Model(Func<int, JArray, CancellationToken, Task<JObject>> handler) { this.handler = handler; }
            public async Task<JObject> CompleteAsync(JArray messages, JArray tools, CancellationToken cancellationToken)
            {
                Requests.Enqueue((JArray)messages.DeepClone());
                int currentActive = Interlocked.Increment(ref active);
                MaxConcurrent = Math.Max(MaxConcurrent, currentActive);
                int index = Interlocked.Increment(ref count);
                try { return await handler(index, messages, cancellationToken); }
                finally { Interlocked.Decrement(ref active); }
            }
        }
        private sealed class Dispatcher : IAgentToolDispatcher
        {
            private readonly Func<string, CancellationToken, Task<JObject>> handler;
            internal readonly ConcurrentQueue<string> Names = new ConcurrentQueue<string>();
            internal Dispatcher(Func<string, CancellationToken, Task<JObject>> handler = null)
            { this.handler = handler ?? ((name, token) => Task.FromResult(Success(name))); }
            public Task<JObject> DispatchAsync(string sessionId, long generation, string toolCallId,
                string toolName, JObject arguments, CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Names.Enqueue(toolName);
                return handler(toolName, cancellationToken);
            }
        }

        private sealed class QueuedDispatcher : IAgentToolDispatcher
        {
            internal sealed class Request
            {
                internal string Session;
                internal long Generation;
                internal string Name;
                internal CancellationToken Token;
                internal readonly TaskCompletionSource<JObject> Result = NewSignal<JObject>();
            }
            internal readonly ConcurrentQueue<Request> Pending = new ConcurrentQueue<Request>();
            internal readonly ConcurrentQueue<string> Executed = new ConcurrentQueue<string>();
            internal Func<string, long, bool> Allows;
            public async Task<JObject> DispatchAsync(string sessionId, long generation, string toolCallId,
                string toolName, JObject arguments, CancellationToken cancellationToken)
            {
                var request = new Request { Session = sessionId, Generation = generation,
                    Name = toolName, Token = cancellationToken };
                Pending.Enqueue(request);
                using (cancellationToken.Register(() => request.Result.TrySetCanceled()))
                    return await request.Result.Task;
            }
            internal void DrainOne()
            {
                if (!Pending.TryDequeue(out Request request)) throw new InvalidOperationException("No queued request.");
                if (request.Token.IsCancellationRequested || !Allows(request.Session, request.Generation))
                    request.Result.TrySetCanceled();
                else
                {
                    Executed.Enqueue(request.Name);
                    request.Result.TrySetResult(Success(request.Name));
                }
            }
        }
    }
}
