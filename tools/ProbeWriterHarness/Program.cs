using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using OniAiProbeMod.Probe;

namespace ProbeWriterHarness
{
    // Exercises ProbeWriter's append path without the game: a transient
    // sharing violation (Bridge rotation, copy tools) must keep records queued
    // and must not raise the sticky process fault (code review 2026-09-26, L3).
    internal static class Program
    {
        private const BindingFlags Static = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;

        private static int Main()
        {
            Type writer = typeof(ProbeWriter);
            string root = Path.Combine(Path.GetTempPath(), "oni-probe-writer-" + Guid.NewGuid().ToString("N"));
            string log = Path.Combine(root, "state.jsonl");
            Directory.CreateDirectory(root);
            try
            {
                writer.GetProperty("StateLogPath", Static).GetSetMethod(true).Invoke(null, new object[] { log });
                var queue = (ConcurrentQueue<IDictionary<string, object>>)writer.GetField("PendingRecords", Static).GetValue(null);
                MethodInfo flush = writer.GetMethod("FlushPendingRecords", Static);
                FieldInfo transientField = writer.GetField("transientOpenFailures", Static);
                Func<int> transient = () => transientField == null ? 0 : (int)transientField.GetValue(null);
                Func<bool> faulted = () => ProbeWriter.HasSafetyFault;

                using (new FileStream(log, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
                {
                    flush.Invoke(null, null);
                    Require(transient() == 0 && !faulted(), "empty queue opened the locked log");

                    queue.Enqueue(Record(1));
                    flush.Invoke(null, null);
                    Require(queue.Count == 1, "a record was dequeued although the log could not be opened");
                    Require(!faulted(), "one transient sharing violation raised the sticky process fault");
                    Require(transient() == 1, "the sharing violation was not counted as transient");
                }

                flush.Invoke(null, null);
                Require(queue.Count == 0 && transient() == 0, "the retained record did not drain");
                Require(!faulted(), "a successful retry left a process fault");
                string[] lines = File.ReadAllText(log).Split(new[] { Environment.NewLine }, StringSplitOptions.None);
                Require(lines.Length == 2 && lines[1].Length == 0 && lines[0].StartsWith("{") && lines[0].EndsWith("}"),
                    "the retained record was not written as one complete line");

                Console.WriteLine("{\"schema\":\"oni.probe-writer-harness.v1\",\"status\":\"passed\",\"cases\":3}");
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
            finally
            {
                try { Directory.Delete(root, true); } catch { }
            }
        }

        private static Dictionary<string, object> Record(long seq)
        {
            return new Dictionary<string, object>
            {
                { "schema", "oni.event.v2" },
                { "event", "diagnostic" },
                { "session_id", "session-writer-harness" },
                { "seq", seq }
            };
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}
