using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Agent
{
    /// <summary>
    /// Bounded Chat Completions history. The runtime owns synchronization. A history unit is
    /// one assistant message and ALL of its tool results, never a partial tool exchange.
    /// Limits are serialized JSON characters, not an inaccurate promise of token counts.
    /// </summary>
    internal sealed class AgentContext
    {
        private readonly int maxMessages;
        private readonly int maxCharacters;
        private readonly int maxResultCharacters;
        private readonly JObject system;
        private readonly string goal;
        private JObject instruction;
        private readonly List<List<JObject>> history = new List<List<JObject>>();

        internal AgentContext(string systemPrompt, string goal, string knowledge,
            int maxMessages, int maxCharacters, int maxResultCharacters)
        {
            if (maxMessages < 4) throw new ArgumentOutOfRangeException(nameof(maxMessages));
            if (maxCharacters < 2048) throw new ArgumentOutOfRangeException(nameof(maxCharacters));
            if (maxResultCharacters < 512) throw new ArgumentOutOfRangeException(nameof(maxResultCharacters));
            if (string.IsNullOrWhiteSpace(goal)) throw new ArgumentException("A task goal is required.", nameof(goal));
            this.maxMessages = maxMessages;
            this.maxCharacters = maxCharacters;
            this.maxResultCharacters = maxResultCharacters;
            this.goal = goal;
            system = Message("system", (systemPrompt ?? string.Empty) +
                (string.IsNullOrWhiteSpace(knowledge) ? string.Empty : "\n\nTask knowledge:\n" + knowledge));
            instruction = Message("user", "Task objective:\n" + goal);
            ValidatePinned(instruction);
        }

        internal void Correct(string correction)
        {
            if (string.IsNullOrWhiteSpace(correction))
                throw new ArgumentException("A correction is required.", nameof(correction));
            var next = Message("user", "Task objective:\n" + goal + "\n\nLatest player correction:\n" + correction +
                "\n\nEarlier actions may have finished before interruption. Re-read the current state before replanning.");
            ValidatePinned(next); // Fail before changing the current instruction.
            // The pinned instruction (right after the system prompt) always holds the
            // latest correction; the correction is also recorded where it happened, so
            // the model sees it once, in order, and not as a new request every turn.
            history.Add(new List<JObject> { Message("user", "Player correction:\n" + correction +
                "\n\nEarlier actions may have finished before interruption. Re-read the current state before replanning.") });
            instruction = next;
            Trim();
        }

        /// <summary>A runtime note to the model (check-in, repetition), kept in order like a correction.</summary>
        internal void AddNote(string note)
        {
            if (string.IsNullOrWhiteSpace(note)) return;
            history.Add(new List<JObject> { Message("user", note) });
            Trim();
        }

        internal JArray Snapshot()
        {
            // Pinned instruction first, then history in order.  Appending it after the
            // latest tool results (as before) re-asked the whole task every turn: live
            // test 2026-11-08 had the model re-observe for 12 turns and never act.
            var messages = new JArray(system.DeepClone(), instruction.DeepClone());
            foreach (var group in history)
                foreach (var message in group) messages.Add(message.DeepClone());
            return messages;
        }

        /// <summary>Must run before executing the assistant's first tool.</summary>
        internal void EnsureTurnFits(JObject assistant)
        {
            var calls = ReadCalls(assistant);
            int count = calls == null ? 0 : calls.Count;
            if (count + 3 > maxMessages || PinnedCost() + Cost(assistant) + count * 513 > maxCharacters)
                throw new InvalidOperationException("The complete tool exchange cannot fit in the context budget.");
        }

        internal void AppendTurn(JObject assistant, IReadOnlyList<JObject> toolResults)
        {
            if (assistant == null || (string)assistant["role"] != "assistant")
                throw new ArgumentException("An assistant message is required.", nameof(assistant));
            var calls = ReadCalls(assistant);
            int count = calls == null ? 0 : calls.Count;
            if (toolResults == null || toolResults.Count != count)
                throw new ArgumentException("Every tool call must have exactly one result.", nameof(toolResults));
            EnsureTurnFits(assistant);

            var group = new List<JObject> { (JObject)assistant.DeepClone() };
            var ids = new HashSet<string>(StringComparer.Ordinal);
            // Divide the remaining budget across this exchange before serializing tool results.
            // Old exchanges can then be trimmed without ever losing a result from this one.
            int perResult = count == 0 ? 0 : Math.Min(maxResultCharacters,
                (maxCharacters - PinnedCost() - Cost(assistant)) / count - 1);
            for (int i = 0; i < count; i++)
            {
                string id = (string)calls[i]["id"];
                if (string.IsNullOrWhiteSpace(id) || id.Length > 128 || !ids.Add(id))
                    throw new ArgumentException("Tool call ids must be unique, nonempty and at most 128 characters.");
                group.Add(ToolMessage(id, toolResults[i] ?? new JObject(), perResult));
            }
            history.Add(group);
            Trim();
        }

        private void ValidatePinned(JObject next)
        {
            // Reserve room for at least an observation and its result; never silently truncate goals.
            if (2 + Cost(system) + Cost(next) + 1024 > maxCharacters)
                throw new ArgumentException("System prompt, knowledge and task instruction exceed the context budget.");
        }

        private int PinnedCost() { return 2 + Cost(system) + Cost(instruction); }

        private void Trim()
        {
            int characters = PinnedCost();
            int messages = 2;
            foreach (var group in history)
                foreach (var message in group) { characters += Cost(message); messages++; }
            while ((characters > maxCharacters || messages > maxMessages) && history.Count > 0)
            {
                foreach (var message in history[0]) { characters -= Cost(message); messages--; }
                history.RemoveAt(0);
            }
        }

        private static int Cost(JObject message) { return message.ToString(Formatting.None).Length + 1; }

        private static JArray ReadCalls(JObject assistant)
        {
            JToken value = assistant["tool_calls"];
            if (value == null || value.Type == JTokenType.Null) return null;
            if (!(value is JArray calls)) throw new ArgumentException("tool_calls must be an array.");
            return calls;
        }

        private static JObject Message(string role, string content)
        {
            return new JObject { ["role"] = role, ["content"] = content };
        }

        private static JObject ToolMessage(string id, JObject result, int limit)
        {
            string serialized = result.ToString(Formatting.None);
            var message = new JObject { ["role"] = "tool", ["tool_call_id"] = id, ["content"] = serialized };
            if (message.ToString(Formatting.None).Length <= limit) return message;

            // The envelope is deliberately valid JSON and advertises missing data. Do not return
            // a chopped JSON document or make a partial result look like the complete game state.
            var truncated = new JObject
            {
                ["truncated"] = true,
                ["original_characters"] = serialized.Length,
                ["notice"] = "Result exceeded context budget. Query smaller pages or a narrower state view.",
                ["preview"] = string.Empty
            };
            int low = 0;
            int high = Math.Min(serialized.Length, limit);
            while (low < high)
            {
                int candidate = low + (high - low + 1) / 2;
                truncated["preview"] = serialized.Substring(0, candidate);
                message["content"] = truncated.ToString(Formatting.None);
                if (message.ToString(Formatting.None).Length <= limit) low = candidate;
                else high = candidate - 1;
            }
            // Avoid splitting a UTF-16 surrogate pair at the preview boundary.
            if (low > 0 && char.IsHighSurrogate(serialized[low - 1])) low--;
            truncated["preview"] = serialized.Substring(0, low);
            message["content"] = truncated.ToString(Formatting.None);
            if (message.ToString(Formatting.None).Length > limit)
                throw new InvalidOperationException("Context budget cannot fit the tool-result truncation envelope.");
            return message;
        }
    }
}
