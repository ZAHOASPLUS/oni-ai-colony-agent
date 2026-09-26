using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;
using OniAiProbeMod.Probe;

namespace WitnessPollHarness
{
    // Code review 2026-09-26 (P2): the executor read the Bridge-confirmation
    // witness every frame, and a read failure re-latched, re-paused and
    // re-logged every frame.  This drives ActionExecutor.Update on a temporary
    // bridge directory: the witness is polled on a cadence, a short sharing
    // violation is retried, and a lasting failure is resolved exactly once.
    internal static class Program
    {
        private const BindingFlags AnyStatic = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
        private static readonly FieldInfo Witness = typeof(ActionExecutor).GetField("pendingWitnessId", AnyStatic);
        private static readonly FieldInfo Latch = typeof(ActionExecutor).GetField("mutationSafetyLatch", AnyStatic);
        private static string confirming;
        private static string done;

        private static int Main()
        {
            string bridge = Path.Combine(Path.GetTempPath(), "oni-witness-poll-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(bridge);
            Environment.SetEnvironmentVariable("ONI_AI_BRIDGE_DIR", bridge);
            try
            {
                List<string> passed = Run(bridge);
                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.witness-poll-harness.v1",
                    ["status"] = "passed",
                    ["cases"] = new JArray(passed.ToArray())
                }.ToString(Newtonsoft.Json.Formatting.None));
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 1;
            }
            finally
            {
                try
                {
                    Directory.Delete(bridge, true);
                }
                catch
                {
                    // Temporary directory only.
                }
            }
        }

        private static List<string> Run(string bridge)
        {
            Require(Witness != null && Latch != null, "executor witness state is missing");
            ProbeWriter.Initialize();
            Require(string.Equals(ProbeWriter.BridgeDirectory, bridge, StringComparison.Ordinal),
                "ProbeWriter escaped the temporary bridge");
            confirming = Path.Combine(bridge, "commands", "confirming");
            done = Path.Combine(bridge, "commands", "done");
            List<string> passed = new List<string>();

            // Cadence: a vanished confirming file is noticed on the next poll, not the next frame.
            StartWaiting("cmd-cadence");
            File.WriteAllText(Path.Combine(confirming, "cmd-cadence.json"), "{}");
            ActionExecutor.Update(10.0f);
            Require(!Latched() && Waiting() == "cmd-cadence", "a present confirming file latched");
            File.Delete(Path.Combine(confirming, "cmd-cadence.json"));
            ActionExecutor.Update(10.2f);
            Require(!Latched() && Waiting() == "cmd-cadence", "the witness was polled again within the cadence");
            ActionExecutor.Update(10.6f);
            Require(Latched() && Waiting() == null, "the next poll did not resolve the vanished witness");
            passed.Add("polled_on_cadence");

            // A short sharing violation on the done file is retried, then confirms.
            StartWaiting("cmd-transient");
            string transientDone = WriteConfirmed("cmd-transient");
            float now = 20f;
            using (new FileStream(transientDone, FileMode.Open, FileAccess.Read, FileShare.None))
            {
                for (int poll = 0; poll < 10; poll++, now += 0.5f)
                {
                    ActionExecutor.Update(now);
                }
                Require(!Latched() && Waiting() == "cmd-transient", "a transient sharing violation latched");
            }
            ActionExecutor.Update(now);
            Require(!Latched() && Waiting() == null, "the confirmed witness was not released after the retry");
            passed.Add("transient_violation_retried");

            // A lasting violation is resolved once: one latch, one error, no repeat.
            StartWaiting("cmd-locked");
            string lockedDone = WriteConfirmed("cmd-locked");
            now = 40f;
            using (new FileStream(lockedDone, FileMode.Open, FileAccess.Read, FileShare.None))
            {
                for (int poll = 0; poll < 40; poll++, now += 0.5f)
                {
                    ActionExecutor.Update(now);
                }
            }
            Require(Latched() && Waiting() == null, "a lasting violation was not resolved as unknown");
            passed.Add("lasting_violation_latched");

            // A malformed confirmation is not transient and latches on the first poll.
            StartWaiting("cmd-malformed");
            File.WriteAllText(Path.Combine(done, "cmd-malformed.json"), "{");
            ActionExecutor.Update(70f);
            Require(Latched() && Waiting() == null, "a malformed confirmation did not latch");
            passed.Add("malformed_latched_on_first_poll");

            ProbeWriter.Shutdown();
            List<JObject> records = File.ReadAllLines(Path.Combine(bridge, "state.jsonl"))
                .Where(line => line.Length > 0)
                .Select(JObject.Parse)
                .ToList();
            int pollErrors = records.Count(record =>
                (string)record["event"] == "probe_error" && (string)record["stage"] == "command_witness_poll");
            string[] latched = records
                .Where(record => (string)record["event"] == "command_safety_latched")
                .Select(record => (string)record["command_id"])
                .ToArray();
            Require(pollErrors == 2,
                "expected one witness error each for the locked and malformed files, saw " + pollErrors);
            Require(latched.SequenceEqual(new[] { "cmd-cadence", "cmd-locked", "cmd-malformed" }),
                "each failure must latch exactly once: " + string.Join(",", latched));
            passed.Add("each_failure_recorded_once");
            return passed;
        }

        // A fresh executor session waiting for the Bridge to confirm commandId.
        private static void StartWaiting(string commandId)
        {
            ActionExecutor.BeginSession();
            Require(!Latched(), "a new session started latched");
            Witness.SetValue(null, commandId);
        }

        private static string WriteConfirmed(string commandId)
        {
            string path = Path.Combine(done, commandId + ".json");
            File.WriteAllText(path, new JObject
            {
                ["id"] = commandId,
                ["status"] = "confirmed",
                ["confirmation"] = new JObject
                {
                    ["confirmation_status"] = "snapshot_confirmed",
                    ["command_id"] = commandId
                }
            }.ToString(Newtonsoft.Json.Formatting.None));
            return path;
        }

        private static string Waiting()
        {
            return (string)Witness.GetValue(null);
        }

        private static bool Latched()
        {
            return (bool)Latch.GetValue(null);
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
