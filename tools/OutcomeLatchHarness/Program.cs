using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;

namespace OutcomeLatchHarness
{
    // Evaluates the production OutcomeLatch on every failed/*.json fixture in
    // one directory so scripts/test_outcome_latch_parity.py can compare it
    // with agent/command_queue.py.  Prints one JSON document to stdout.
    internal static class Program
    {
        private static int Main(string[] args)
        {
            if (args.Length != 1 || !Directory.Exists(args[0]))
            {
                Console.Error.WriteLine("usage: OutcomeLatchHarness <failed-directory>");
                return 2;
            }

            JObject files = new JObject();
            foreach (string path in Directory.GetFiles(args[0], "*.json"))
            {
                bool readable = OutcomeLatch.TryReadFailedRecord(path, out OutcomeLatch.FailedRecord record);
                files[Path.GetFileName(path)] = new JObject
                {
                    ["readable"] = readable,
                    ["latches"] = !readable || OutcomeLatch.IsUnresolved(record)
                };
            }

            Console.WriteLine(new JObject
            {
                ["schema"] = "oni.outcome-latch-harness.v1",
                ["first_unresolved"] = OutcomeLatch.FindUnresolvedFailure(args[0]),
                ["signature_lines"] = OutcomeLatch.DirectorySignature(args[0]).Split('\n').Length - 1,
                ["files"] = files
            }.ToString(Formatting.None));
            return 0;
        }
    }
}
