using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent
{
    /// <summary>
    /// Implementations queue a DTO for the Unity main thread and return without blocking it.
    /// Immediately BEFORE invoking a handler, revalidate session, generation, running state
    /// and the write lease. Cancellation must remove/skip queued work. A running handler may
    /// safely finish, but its stale continuation must never execute another handler.
    /// </summary>
    internal interface IAgentToolDispatcher
    {
        Task<JObject> DispatchAsync(string sessionId, long generation, string toolCallId,
            string toolName, JObject arguments, CancellationToken cancellationToken);
    }

    internal enum AgentRuntimeState { Stopped, Running, Paused, Interrupting, Faulted, Disposed }

    internal sealed class AgentRuntimeOptions
    {
        internal string SystemPrompt { get; set; } =
            "You operate an Oxygen Not Included test colony through the advertised tools. " +
            "Use current observed state, respect tool errors and the player's latest instructions. " +
            "Give concise public decision summaries, not hidden reasoning. Request smaller pages " +
            "when results are truncated. Never treat a claimed achievement as verified without game evidence. " +
            "After interruption, do not replay earlier queued actions; replan from fresh state.";
        internal string Knowledge { get; set; } = string.Empty;
        internal int MaxModelTurns { get; set; } = 24;
        internal int MaxToolCallsPerTurn { get; set; } = 8;
        internal int MaxToolCallsTotal { get; set; } = 128;
        internal int MaxContextMessages { get; set; } = 80;
        internal int MaxContextCharacters { get; set; } = 65536;
        internal int MaxToolResultCharacters { get; set; } = 8192;
        internal int MaxAssistantCharacters { get; set; } = 16384;
        internal TimeSpan MaxSessionDuration { get; set; } = TimeSpan.FromMinutes(30);
        internal TimeSpan ModelTurnDelay { get; set; } = TimeSpan.FromMilliseconds(250);
        internal string RefreshToolName { get; set; } = "get_lifecycle";
        internal JObject RefreshToolArguments { get; set; } = new JObject();
        /// <summary>Text-only replies check in again after ContinueDelay instead of pausing.</summary>
        internal bool Continuous { get; set; }
        internal TimeSpan ContinueDelay { get; set; } = TimeSpan.FromSeconds(30);
        /// <summary>Transient model failures retried before pausing; 0 pauses on the first one.</summary>
        internal int MaxConsecutiveModelErrors { get; set; } = 6;
        internal TimeSpan ModelErrorBaseDelay { get; set; } = TimeSpan.FromSeconds(10);
        internal TimeSpan MaxModelErrorDelay { get; set; } = TimeSpan.FromMinutes(5);
        internal int RefreshAttempts { get; set; } = 3;
        internal TimeSpan RefreshRetryDelay { get; set; } = TimeSpan.FromSeconds(2);

        internal AgentRuntimeOptions ValidatedCopy()
        {
            if (MaxModelTurns < 1 || MaxToolCallsPerTurn < 1 || MaxToolCallsPerTurn > 64 ||
                MaxToolCallsTotal < 1 || MaxContextMessages < MaxToolCallsPerTurn + 3 ||
                MaxContextCharacters < 2048 || MaxToolResultCharacters < 512 || MaxAssistantCharacters < 512)
                throw new ArgumentOutOfRangeException(nameof(AgentRuntimeOptions), "Invalid runtime budget.");
            if (MaxSessionDuration <= TimeSpan.Zero || MaxSessionDuration.TotalMilliseconds > int.MaxValue ||
                ModelTurnDelay < TimeSpan.Zero || ModelTurnDelay.TotalMilliseconds > int.MaxValue ||
                ContinueDelay < TimeSpan.Zero || ContinueDelay.TotalMilliseconds > int.MaxValue ||
                ModelErrorBaseDelay < TimeSpan.Zero || MaxModelErrorDelay < TimeSpan.Zero ||
                MaxModelErrorDelay.TotalMilliseconds > int.MaxValue || MaxConsecutiveModelErrors < 0 ||
                RefreshAttempts < 1 || RefreshRetryDelay < TimeSpan.Zero || RefreshRetryDelay.TotalMilliseconds > int.MaxValue)
                throw new ArgumentOutOfRangeException(nameof(AgentRuntimeOptions), "Invalid runtime time limit.");
            if (string.IsNullOrWhiteSpace(RefreshToolName))
                throw new ArgumentException("A read-only refresh tool is required.", nameof(RefreshToolName));
            var copy = (AgentRuntimeOptions)MemberwiseClone();
            copy.RefreshToolArguments = RefreshToolArguments == null ? new JObject() :
                (JObject)RefreshToolArguments.DeepClone();
            return copy;
        }
    }

    internal sealed class AgentRuntimeEvent
    {
        internal AgentRuntimeEvent(string kind, string message, string sessionId, long generation,
            string toolName = null, string toolCallId = null, string data = null)
        {
            Kind = kind; Message = message; SessionId = sessionId; Generation = generation;
            ToolName = toolName; ToolCallId = toolCallId; Data = data; TimestampUtc = System.DateTime.UtcNow;
        }
        internal string Kind { get; }
        internal string Message { get; }
        internal string SessionId { get; }
        internal long Generation { get; }
        internal string ToolName { get; }
        internal string ToolCallId { get; }
        internal string Data { get; }
        internal System.DateTime TimestampUtc { get; }
    }

    /// <summary>
    /// One persistent background worker. Start/correct/pause/stop never wait for network or
    /// game work. A non-cooperative model request delays the next generation, rather than
    /// spawning overlapping requests. The supplied client must honor cancellation/timeouts.
    /// Dispose requests shutdown; Completion can be awaited off the Unity thread for teardown.
    /// </summary>
    internal sealed class AgentRuntime : IDisposable
    {
        private sealed class Run
        {
            internal string Session;
            internal long Generation;
            internal AgentContext Context;
            internal readonly CancellationTokenSource Source = new CancellationTokenSource();
            internal bool Executing;
            internal Task Cancellation;
        }

        private readonly object gate = new object();
        private readonly SemaphoreSlim wake = new SemaphoreSlim(0, 1);
        private readonly IAgentModelClient client;
        private readonly IAgentToolDispatcher dispatcher;
        private readonly AgentRuntimeOptions options;
        private readonly JArray tools;
        private readonly HashSet<string> toolNames = new HashSet<string>(StringComparer.Ordinal);
        private readonly Task worker;
        private Run current;
        private AgentContext context;
        private AgentRuntimeState state = AgentRuntimeState.Stopped;
        private string sessionId;
        private long generation;
        private Stopwatch elapsed;
        private int modelTurns;
        private int toolCalls;
        private int observations;
        private int repeats;
        private string lastSignature;

        internal AgentRuntime(IAgentModelClient client, IAgentToolDispatcher dispatcher, JArray tools,
            AgentRuntimeOptions options = null)
        {
            this.client = client ?? throw new ArgumentNullException(nameof(client));
            this.dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
            this.options = (options ?? new AgentRuntimeOptions()).ValidatedCopy();
            this.tools = tools == null ? throw new ArgumentNullException(nameof(tools)) : (JArray)tools.DeepClone();
            foreach (JToken tool in this.tools)
            {
                string name = (string)tool["function"]?["name"];
                if (string.IsNullOrWhiteSpace(name) || !toolNames.Add(name))
                    throw new ArgumentException("Tool schema names must be nonempty and unique.", nameof(tools));
            }
            if (!toolNames.Contains(this.options.RefreshToolName))
                throw new ArgumentException("The read-only refresh tool must be present in the schema.", nameof(tools));
            worker = Task.Run(WorkerAsync);
        }

        internal event Action<AgentRuntimeEvent> EventRaised;
        internal AgentRuntimeState State { get { lock (gate) return state; } }
        internal long Generation { get { lock (gate) return generation; } }
        internal string SessionId { get { lock (gate) return sessionId; } }
        internal Task Completion { get { return worker; } }

        internal bool CanDispatch(string session, long taskGeneration)
        {
            lock (gate)
                return state == AgentRuntimeState.Running && current != null &&
                    string.Equals(sessionId, session, StringComparison.Ordinal) && generation == taskGeneration &&
                    !current.Source.IsCancellationRequested;
        }

        internal void Start(string session, string goal)
        {
            if (string.IsNullOrWhiteSpace(session)) throw new ArgumentException("A session id is required.", nameof(session));
            // Validate user inputs before interrupting a valid task.
            var nextContext = new AgentContext(options.SystemPrompt, goal, options.Knowledge,
                options.MaxContextMessages, options.MaxContextCharacters, options.MaxToolResultCharacters);
            AgentRuntimeEvent notification;
            lock (gate)
            {
                ThrowIfDisposed();
                RetireLocked();
                generation = checked(generation + 1);
                sessionId = session;
                context = nextContext;
                ResetBudgetLocked();
                BeginLocked();
                notification = EventLocked("started", "Agent started; reading fresh state.");
            }
            Publish(notification);
            Signal();
        }

        internal void Correct(string instruction)
        {
            AgentRuntimeEvent notification;
            lock (gate)
            {
                ThrowIfDisposed();
                if (context == null || state == AgentRuntimeState.Stopped)
                    throw new InvalidOperationException("Start an Agent task before sending a correction.");
                context.Correct(instruction);
                state = AgentRuntimeState.Interrupting;
                RetireLocked();
                generation = checked(generation + 1);
                // A player instruction is a deliberate go-ahead: give it a fresh budget and
                // keep the context.  (Keeping the spent budget made a correction after an
                // exhausted budget stop again at once, with Start, which drops the context,
                // as the only way out.)
                ResetBudgetLocked();
                BeginLocked();
                notification = EventLocked("corrected", "Correction accepted; old queued work is invalid and current state will be re-read.");
            }
            Publish(notification);
            Signal();
        }

        internal void Pause()
        {
            AgentRuntimeEvent notification;
            lock (gate)
            {
                ThrowIfDisposed();
                if (state != AgentRuntimeState.Running) return;
                state = AgentRuntimeState.Paused;
                generation = checked(generation + 1);
                RetireLocked();
                notification = EventLocked("paused", "Agent paused. Game speed was not changed.");
            }
            Publish(notification);
        }

        internal void Resume()
        {
            AgentRuntimeEvent notification;
            lock (gate)
            {
                ThrowIfDisposed();
                if (state == AgentRuntimeState.Running) return;
                if (state != AgentRuntimeState.Paused || context == null)
                    throw new InvalidOperationException("Only a paused Agent task can be resumed.");
                generation = checked(generation + 1);
                ResetBudgetLocked(); // Same reasoning as Correct: resuming is a go-ahead.
                BeginLocked();
                notification = EventLocked("resumed", "Agent resumed; reading fresh state.");
            }
            Publish(notification);
            Signal();
        }

        internal void Stop()
        {
            AgentRuntimeEvent notification;
            lock (gate)
            {
                if (state == AgentRuntimeState.Disposed || state == AgentRuntimeState.Stopped) return;
                state = AgentRuntimeState.Stopped;
                generation = checked(generation + 1);
                RetireLocked();
                notification = EventLocked("stopped", "Agent stopped. Pending old work is invalid.");
            }
            Publish(notification);
        }

        public void Dispose()
        {
            lock (gate)
            {
                if (state == AgentRuntimeState.Disposed) return;
                state = AgentRuntimeState.Disposed;
                generation = checked(generation + 1);
                RetireLocked();
            }
            Signal();
        }

        private void ResetBudgetLocked()
        {
            modelTurns = 0;
            toolCalls = 0;
            repeats = 0;
            lastSignature = null;
            elapsed = Stopwatch.StartNew();
        }

        private void BeginLocked()
        {
            current = new Run { Session = sessionId, Generation = generation, Context = context };
            TimeSpan remaining = options.MaxSessionDuration - elapsed.Elapsed;
            current.Source.CancelAfter(remaining > TimeSpan.Zero ? remaining : TimeSpan.Zero);
            state = AgentRuntimeState.Running;
        }

        private void RetireLocked()
        {
            Run old = current;
            current = null;
            if (old == null) return;
            bool executing = old.Executing;
            // Cancellation callbacks belong to arbitrary transports/dispatchers. Never run them
            // inline on the UI thread while holding the runtime gate.
            old.Cancellation = Task.Run(() =>
            {
                try { old.Source.Cancel(); }
                catch (AggregateException) { /* A broken cancellation callback cannot revive old work. */ }
                finally { if (!executing) old.Source.Dispose(); }
            });
        }

        private async Task WorkerAsync()
        {
            while (true)
            {
                await wake.WaitAsync().ConfigureAwait(false);
                while (true)
                {
                    Run run;
                    lock (gate)
                    {
                        if (state == AgentRuntimeState.Disposed) return;
                        if (state != AgentRuntimeState.Running || current == null) break;
                        run = current;
                        run.Executing = true;
                    }
                    try { await ExecuteAsync(run).ConfigureAwait(false); }
                    catch (OperationCanceledException)
                    {
                        if (IsCurrent(run) && run.Source.IsCancellationRequested)
                            End(run, AgentRuntimeState.Paused, "session_budget_exhausted", "The task time budget was exhausted.");
                        else if (IsCurrent(run))
                            End(run, AgentRuntimeState.Faulted, "operation_cancelled", "A model or tool operation cancelled unexpectedly.");
                        else Publish(Event(run, "generation_cancelled", "Cancelled work from an older generation."));
                    }
                    catch (AgentModelException error)
                    {
                        // These fields are created by our transport, not copied from the provider
                        // response body. Keep actionable status without exposing arbitrary text.
                        End(run, AgentRuntimeState.Faulted, "model_error", "Model request failed (" + error.Code +
                            (error.StatusCode.HasValue ? "; HTTP " + error.StatusCode.Value : string.Empty) + ").");
                    }
                    catch (Exception error)
                    {
                        // Do not surface arbitrary exception text: providers may echo credentials.
                        End(run, AgentRuntimeState.Faulted, "runtime_fault", "Agent stopped on " + error.GetType().Name + ".");
                    }
                    finally
                    {
                        Task cancellation;
                        lock (gate) cancellation = run.Cancellation;
                        if (cancellation != null) await cancellation.ConfigureAwait(false);
                        run.Source.Dispose();
                    }
                    // Controls can replace the generation while an older request is finishing.
                    // The new one runs on this same worker, never in a parallel model loop.
                }
            }
        }

        private async Task ExecuteAsync(Run run)
        {
            if (!CheckBudget(run)) return;
            if (!await ObserveAsync(run).ConfigureAwait(false)) return;

            int idleCheckIns = 0;
            while (CheckBudget(run))
            {
                JArray messages;
                lock (gate)
                {
                    if (!IsCurrentLocked(run)) return;
                    modelTurns++;
                    messages = run.Context.Snapshot();
                }
                JObject raw = await CompleteWithRetryAsync(run, messages).ConfigureAwait(false);
                if (raw == null) return; // Ended by the retry policy or a newer generation.
                if (!IsCurrent(run) || run.Source.IsCancellationRequested)
                {
                    Publish(Event(run, "stale_response_discarded", "Ignored a model response from an invalid generation."));
                    run.Source.Token.ThrowIfCancellationRequested();
                    return;
                }
                JObject assistant = NormalizeAssistant(run, raw);
                JArray calls = assistant["tool_calls"] as JArray;
                lock (gate)
                {
                    if (!IsCurrentLocked(run)) return;
                    run.Context.EnsureTurnFits(assistant);
                }
                string publicText = (string)assistant["content"];
                if (!string.IsNullOrWhiteSpace(publicText))
                    Publish(Event(run, "assistant_message", publicText));
                if (calls == null || calls.Count == 0)
                {
                    lock (gate)
                    {
                        if (!IsCurrentLocked(run)) return;
                        run.Context.AppendTurn(assistant, new JObject[0]);
                    }
                    if (!options.Continuous)
                    {
                        End(run, AgentRuntimeState.Paused, "awaiting_instruction",
                            "Model returned without tool calls. Any claimed completion remains unverified.");
                        return;
                    }
                    // Continuous play: a text-only reply is a report, not the end of the
                    // game.  Let the colony run, then check in with fresh state.
                    if (!CheckBudget(run)) return; // Do not wait and re-read just to stop.
                    idleCheckIns++;
                    Publish(Event(run, "idle", "No tool calls; checking in again in " +
                        (int)options.ContinueDelay.TotalSeconds + " s."));
                    await Task.Delay(options.ContinueDelay, run.Source.Token).ConfigureAwait(false);
                    lock (gate)
                    {
                        if (!IsCurrentLocked(run)) return;
                        run.Context.AddNote("Check-in " + idleCheckIns + ": the game kept running since your last reply. " +
                            "Read what changed, then keep working toward the objective. Reply without tool calls only " +
                            "when there is truly nothing useful to do until the next check-in.");
                    }
                    if (!await ObserveAsync(run).ConfigureAwait(false)) return;
                    continue;
                }
                idleCheckIns = 0;

                var results = new List<JObject>();
                foreach (JToken call in calls)
                {
                    if (!IsCurrent(run)) return; // Discard this incomplete exchange, never replay its remainder.
                    run.Source.Token.ThrowIfCancellationRequested();
                    string id = (string)call["id"];
                    string name = (string)call["function"]["name"];
                    JObject args;
                    JObject result;
                    if (!toolNames.Contains(name)) result = Error("unknown_tool", "Tool is not present in the advertised schema.");
                    else if (!TryArguments(call["function"]["arguments"], out args))
                        result = Error("invalid_arguments", "Tool arguments must be a JSON object encoded as a string.");
                    else result = await DispatchAsync(run, id, name, args).ConfigureAwait(false);
                    results.Add(result);
                }
                lock (gate)
                {
                    if (!IsCurrentLocked(run)) return;
                    run.Context.AppendTurn(assistant, results);
                    NoteRepetitionLocked(run, calls);
                }
                if (options.ModelTurnDelay > TimeSpan.Zero)
                    await Task.Delay(options.ModelTurnDelay, run.Source.Token).ConfigureAwait(false);
            }
        }

        /// <summary>
        /// Same calls with the same arguments, turn after turn, mean the model is not
        /// using what it already read (live test 2026-11-08: 12 turns of identical
        /// get_status/get_events/get_map).  Say so once it repeats; the budget still bounds it.
        /// </summary>
        private void NoteRepetitionLocked(Run run, JArray calls)
        {
            var signature = new System.Text.StringBuilder();
            foreach (JToken call in calls)
                signature.Append((string)call["function"]["name"]).Append('(')
                    .Append((string)call["function"]["arguments"]).Append(");");
            string current = signature.ToString();
            repeats = current == lastSignature ? repeats + 1 : 0;
            lastSignature = current;
            if (repeats >= 2)
                run.Context.AddNote("You have issued exactly the same tool calls " + (repeats + 1) +
                    " turns in a row. Their results are already above. Act on them, or change approach.");
        }

        /// <summary>
        /// Transient model failures (timeouts, 429/5xx, network, malformed or empty
        /// replies) wait and retry instead of faulting the whole task; only errors that
        /// retrying cannot fix (authentication, bad endpoint or model) fault at once.
        /// </summary>
        private async Task<JObject> CompleteWithRetryAsync(Run run, JArray messages)
        {
            for (int failures = 0; ; failures++)
            {
                try
                {
                    return await client.CompleteAsync(messages, (JArray)tools.DeepClone(), run.Source.Token).ConfigureAwait(false);
                }
                catch (AgentModelException error) when (IsTransient(error) && failures < options.MaxConsecutiveModelErrors)
                {
                    TimeSpan delay = TimeSpan.FromTicks(Math.Min(options.MaxModelErrorDelay.Ticks,
                        options.ModelErrorBaseDelay.Ticks * (1L << Math.Min(failures, 16))));
                    Publish(Event(run, "model_retry", "Model request failed (" + error.Code +
                        (error.StatusCode.HasValue ? "; HTTP " + error.StatusCode.Value : string.Empty) +
                        "); retrying in " + (int)delay.TotalSeconds + " s."));
                    await Task.Delay(delay, run.Source.Token).ConfigureAwait(false);
                    if (!IsCurrent(run)) return null;
                }
                catch (AgentModelException error) when (IsTransient(error))
                {
                    // Out of retries: pause (releasing writes) rather than fault, so Resume works.
                    End(run, AgentRuntimeState.Paused, "model_unavailable", "Model kept failing (" + error.Code +
                        (error.StatusCode.HasValue ? "; HTTP " + error.StatusCode.Value : string.Empty) +
                        "). Agent paused; Resume to try again.");
                    return null;
                }
            }
        }

        private static bool IsTransient(AgentModelException error)
        {
            switch (error.Code)
            {
                case "http_error":
                    int status = error.StatusCode ?? 0;
                    return status == 408 || status == 409 || status == 425 || status == 429 || status >= 500;
                case "network_error":
                case "timeout":
                case "request_failed":
                case "retry_limit":
                case "incomplete_response":
                case "empty_response":
                case "empty_assistant_message":
                case "invalid_response_json":
                case "invalid_response_encoding":
                case "invalid_assistant_message":
                case "response_too_large":
                    return true;
                default:
                    return false;
            }
        }

        /// <returns>false when the run ended or was replaced.</returns>
        private async Task<bool> ObserveAsync(Run run)
        {
            string observationId = "state_" + run.Generation + "_" + Interlocked.Increment(ref observations);
            for (int attempt = 1; ; attempt++)
            {
                var observation = new JObject
                {
                    ["role"] = "assistant", ["content"] = "Read current game state before planning.",
                    ["tool_calls"] = new JArray(new JObject
                    {
                        ["id"] = observationId + "_" + attempt, ["type"] = "function",
                        ["function"] = new JObject { ["name"] = options.RefreshToolName,
                            ["arguments"] = options.RefreshToolArguments.ToString(Formatting.None) }
                    })
                };
                lock (gate)
                {
                    if (!IsCurrentLocked(run)) return false;
                    run.Context.EnsureTurnFits(observation);
                }
                JObject observed = await DispatchAsync(run, observationId + "_" + attempt, options.RefreshToolName,
                    (JObject)options.RefreshToolArguments.DeepClone()).ConfigureAwait(false);
                if (!IsCurrent(run)) return false;
                // A failed refresh is not current state: never plan on it.  One failed read
                // used to pause the task; retry a few times first.
                if (observed["error"] == null)
                {
                    lock (gate)
                    {
                        if (!IsCurrentLocked(run)) return false;
                        run.Context.AppendTurn(observation, new[] { observed });
                    }
                    return true;
                }
                if (attempt >= options.RefreshAttempts)
                {
                    End(run, AgentRuntimeState.Paused, "state_refresh_failed", "Unable to read fresh game state. Agent paused.");
                    return false;
                }
                await Task.Delay(options.RefreshRetryDelay, run.Source.Token).ConfigureAwait(false);
            }
        }

        private async Task<JObject> DispatchAsync(Run run, string id, string name, JObject arguments)
        {
            lock (gate)
            {
                if (!IsCurrentLocked(run)) throw new OperationCanceledException();
                run.Source.Token.ThrowIfCancellationRequested();
                if (toolCalls >= options.MaxToolCallsTotal)
                    return Error("tool_budget_exhausted", "No tool calls remain in the task budget.");
                toolCalls++;
            }
            Publish(Event(run, "tool_started", "Dispatching " + name + ".", name, id, BoundedEventData(arguments)));
            JObject result;
            try
            {
                result = await dispatcher.DispatchAsync(run.Session, run.Generation, id, name,
                    arguments, run.Source.Token).ConfigureAwait(false);
                result = result ?? Error("empty_tool_result", "Tool dispatcher returned no result.");
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception error)
            {
                result = Error("tool_execution_failed", "Dispatcher failed with " + error.GetType().Name + ".");
            }
            // Record completed work even if the player corrected during the actual handler.
            Publish(Event(run, IsCurrent(run) ? "tool_completed" : "tool_completed_stale",
                "Tool returned: " + name + ".", name, id, BoundedEventData(result)));
            return result;
        }

        private string BoundedEventData(JObject value)
        {
            string data = value.ToString(Formatting.None);
            if (data.Length <= options.MaxToolResultCharacters) return data;
            JObject summary = new JObject { ["truncated"] = true, ["original_characters"] = data.Length,
                ["notice"] = "Tool event data exceeded its bounded console limit." };
            JObject metadata = value["_agent_dispatch"] as JObject;
            if (metadata != null)
            {
                JObject metrics = new JObject();
                foreach (string field in new[] { "execution_ms", "game_cycle" })
                    if (metadata[field]?.Type == JTokenType.Integer || metadata[field]?.Type == JTokenType.Float)
                        metrics[field] = metadata[field].DeepClone();
                summary["_agent_dispatch"] = metrics;
            }
            return summary.ToString(Formatting.None);
        }

        /// <summary>
        /// Repairs instead of rejecting: an oversized batch is cut to the per-turn limit,
        /// a bad or duplicate call id is replaced, an unusable name becomes an unknown
        /// tool (answered with an error), and overlong text is shortened.  Each of these
        /// used to fault the whole task.
        /// </summary>
        private JObject NormalizeAssistant(Run run, JObject raw)
        {
            if (raw == null || (string)raw["role"] != "assistant")
                throw new AgentModelException("invalid_assistant_message");
            JToken content = raw["content"];
            string text = content != null && content.Type == JTokenType.String ? (string)content : null;
            int textLimit = options.MaxAssistantCharacters / 2;
            if (text != null && text.Length > textLimit)
            {
                text = text.Substring(0, textLimit) + " [shortened]";
                Publish(Event(run, "assistant_shortened", "Assistant text exceeded " + textLimit + " characters and was shortened."));
            }
            var normalized = new JObject { ["role"] = "assistant", ["content"] = text == null ? JValue.CreateNull() : new JValue(text) };
            JArray calls = raw["tool_calls"] as JArray;
            if (calls == null || calls.Count == 0) return normalized;
            if (calls.Count > options.MaxToolCallsPerTurn)
                Publish(Event(run, "tool_calls_truncated", "Model sent " + calls.Count + " tool calls; only the first " +
                    options.MaxToolCallsPerTurn + " run this turn."));
            var ids = new HashSet<string>(StringComparer.Ordinal);
            var cleanCalls = new JArray();
            for (int i = 0; i < calls.Count && cleanCalls.Count < options.MaxToolCallsPerTurn; i++)
            {
                var function = calls[i]?["function"] as JObject;
                string id = calls[i]?["id"]?.Type == JTokenType.String ? (string)calls[i]["id"] : null;
                if (string.IsNullOrWhiteSpace(id) || id.Length > 128 || !ids.Add(id))
                {
                    id = "call_" + run.Generation + "_" + i;
                    ids.Add(id);
                }
                string name = function?["name"]?.Type == JTokenType.String ? (string)function["name"] : null;
                if (string.IsNullOrWhiteSpace(name) || name.Length > 64) name = "invalid_tool_name";
                JToken arguments = function?["arguments"];
                cleanCalls.Add(new JObject { ["id"] = id, ["type"] = "function", ["function"] =
                    new JObject { ["name"] = name, ["arguments"] = arguments?.DeepClone() ?? new JValue("{}") } });
            }
            normalized["tool_calls"] = cleanCalls;
            return normalized;
        }

        private static bool TryArguments(JToken value, out JObject arguments)
        {
            arguments = null;
            if (value == null || value.Type != JTokenType.String) return false;
            try { arguments = JObject.Parse((string)value); return true; }
            catch (JsonException) { return false; }
        }

        private bool CheckBudget(Run run)
        {
            bool exhausted;
            lock (gate)
            {
                if (!IsCurrentLocked(run)) return false;
                exhausted = modelTurns >= options.MaxModelTurns || toolCalls >= options.MaxToolCallsTotal ||
                    elapsed.Elapsed >= options.MaxSessionDuration;
            }
            if (exhausted) End(run, AgentRuntimeState.Paused, "budget_exhausted", "Configured task budget reached. Start a new task to reset it.");
            return !exhausted;
        }

        private bool IsCurrent(Run run) { lock (gate) return IsCurrentLocked(run); }
        private bool IsCurrentLocked(Run run)
        {
            return state == AgentRuntimeState.Running && ReferenceEquals(run, current) &&
                generation == run.Generation && string.Equals(sessionId, run.Session, StringComparison.Ordinal);
        }

        private void End(Run run, AgentRuntimeState nextState, string kind, string message)
        {
            AgentRuntimeEvent notification;
            lock (gate)
            {
                if (!IsCurrentLocked(run)) return;
                state = nextState;
                generation = checked(generation + 1);
                RetireLocked();
                notification = Event(run, kind, message);
            }
            Publish(notification);
        }

        private static JObject Error(string code, string message)
        {
            return new JObject { ["error"] = new JObject { ["code"] = code, ["message"] = message } };
        }
        private AgentRuntimeEvent EventLocked(string kind, string message)
        { return new AgentRuntimeEvent(kind, message, sessionId, generation); }
        private static AgentRuntimeEvent Event(Run run, string kind, string message,
            string tool = null, string id = null, string data = null)
        { return new AgentRuntimeEvent(kind, message, run.Session, run.Generation, tool, id, data); }
        private void ThrowIfDisposed()
        { if (state == AgentRuntimeState.Disposed) throw new ObjectDisposedException(nameof(AgentRuntime)); }
        private void Signal()
        { try { wake.Release(); } catch (SemaphoreFullException) { /* A wake is already queued. */ } }
        private void Publish(AgentRuntimeEvent notification)
        {
            var subscribers = EventRaised;
            if (subscribers == null) return;
            foreach (Action<AgentRuntimeEvent> subscriber in subscribers.GetInvocationList())
                try { subscriber(notification); } catch { /* UI/audit failure cannot break cancellation or loop ownership. */ }
        }
    }
}
