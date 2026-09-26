using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Commands;
using OniAiProbeMod.Security;

namespace ExecutionClaimHarness
{
    internal static class Program
    {
        private const string AttemptId = "attempt-11111111111111111111111111111111";
        private const string IntentHash =
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            string root = Path.Combine(Path.GetTempPath(), "oni-execution-claim-" + Guid.NewGuid().ToString("N"));
            try
            {
                Directory.CreateDirectory(root);
                CrashBeforeClaim(Path.Combine(root, "before-claim"));
                CrashAfterClaimBeforeAck(Path.Combine(root, "after-claim-before-ack"));
                CrashAfterAckBeforeDispatch(Path.Combine(root, "after-ack-before-dispatch"));
                CrashAfterDispatch(Path.Combine(root, "after-dispatch"));
                CrashAfterDirectVerify(Path.Combine(root, "after-direct-verify"));
                CrashAfterBridgePrepareBeforeRename(Path.Combine(root, "after-bridge-prepare"));
                BridgeRenameDuringStartupEnumeration(Path.Combine(root, "bridge-rename-race"));
                RejectIntentTampering(Path.Combine(root, "intent-tamper"));

                Console.WriteLine(new JObject
                {
                    ["schema"] = "oni.execution-claim-harness.v1",
                    ["status"] = "passed",
                    ["passed_count"] = Passed.Count,
                    ["cases"] = new JArray(Passed)
                }.ToString(Formatting.Indented));
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
                    Directory.Delete(root, true);
                }
                catch
                {
                    // The harness root is uniquely named under the OS temp dir.
                }
            }
        }

        private static void CrashBeforeClaim(string root)
        {
            Layout layout = CreateLayout(root, "cmd-before-claim");
            int dispatchCount = 0;
            Require(File.Exists(layout.ApprovedPath), "approved input disappeared before claim");
            Require(!File.Exists(layout.ExecutingPath), "executing claim exists before claim call");
            Require(dispatchCount == 0, "dispatch occurred before claim");
            Passed.Add("crash_before_claim_keeps_unclaimed_input_and_never_dispatches");
        }

        private static void CrashAfterClaimBeforeAck(string root)
        {
            Layout layout = CreateLayout(root, "cmd-after-claim-before-ack");
            JObject claimed = Claim(layout);
            JObject claim = RequireClaim(claimed);
            Require(claim["accepted_ack"]?["state"]?.ToString() == "not_enqueued", "pre-ACK claim lied about ACK state");
            Require(claim["dispatch"]?["state"]?.ToString() == "not_started", "pre-ACK claim crossed dispatch boundary");
            RecoverExecutingAsUnknown(layout, claimed);
            Passed.Add("crash_after_claim_before_ack_recovers_outcome_unknown_and_latches");
        }

        private static void CrashAfterAckBeforeDispatch(string root)
        {
            Layout layout = CreateLayout(root, "cmd-after-ack-before-dispatch");
            JObject claimed = Claim(layout);
            RecordAcceptedAck(claimed, 41);
            Persist(layout, claimed);
            JObject stored = Load(layout.ExecutingPath);
            Require(stored["accepted_ack_seq"]?.Value<long>() == 41L, "accepted ACK sequence was not persisted");
            Require(
                stored["execution_claim"]?["accepted_ack"]?["durability"]?.ToString() ==
                ExecutionClaimStore.AsyncAckDurability,
                "accepted ACK was not labelled as async/unconfirmed");
            Require(stored["execution_claim"]?["dispatch"]?["state"]?.ToString() == "authorized_not_started", "dispatch state is wrong");
            int dispatchCount = 0;
            Require(dispatchCount == 0, "dispatch occurred in ACK-before-dispatch boundary");
            RecoverExecutingAsUnknown(layout, stored);
            Passed.Add("crash_after_ack_before_dispatch_keeps_async_ack_honest_and_recovers_unknown");
        }

        private static void CrashAfterDispatch(string root)
        {
            Layout layout = CreateLayout(root, "cmd-after-dispatch");
            JObject claimed = Claim(layout);
            RecordAcceptedAck(claimed, 51);
            Persist(layout, claimed);
            int dispatchCount = 0;
            dispatchCount++;
            claimed["execution_claim"]["dispatch"] = new JObject
            {
                ["state"] = "returned",
                ["returned_at"] = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                ["reported_success"] = true
            };
            Persist(layout, claimed);
            Require(dispatchCount == 1, "simulated mutating dispatch count changed");
            RecoverExecutingAsUnknown(layout, Load(layout.ExecutingPath));
            Passed.Add("crash_after_dispatch_never_replays_and_recovers_outcome_unknown");
        }

        private static void CrashAfterDirectVerify(string root)
        {
            Layout layout = CreateLayout(root, "cmd-after-direct-verify");
            JObject claimed = Claim(layout);
            RecordAcceptedAck(claimed, 61);
            claimed["executed_ack_seq"] = 62;
            claimed["local_effect_ack_seq"] = 63;
            claimed["execution_claim"]["dispatch"] = new JObject
            {
                ["state"] = "returned",
                ["reported_success"] = true
            };
            claimed["execution_claim"]["direct_verification"] = new JObject
            {
                ["state"] = "local_effect_observed",
                ["ack_seq"] = 63,
                ["ack_durability"] = ExecutionClaimStore.AsyncAckDurability
            };
            claimed["status"] = "executed_pending_snapshot";
            bool transitioned = ExecutionClaimStore.TryTransitionExecuting(
                layout.ExecutingPath,
                layout.Confirming,
                claimed,
                AttemptId,
                IntentHash,
                out string confirmingPath,
                out string error);
            Require(transitioned, "direct verification transition failed: " + error);
            Require(!File.Exists(layout.ExecutingPath), "executing claim remained after confirming transition");
            JObject confirming = Load(confirmingPath);
            Require(confirming["status"]?.ToString() == "executed_pending_snapshot", "confirming status changed");
            Require(confirming["execution_claim"]?["direct_verification"]?["ack_seq"]?.Value<long>() == 63L, "direct-read evidence was lost");

            // Startup treats any mutating confirming record as unknown until an
            // independent snapshot attestor committed it. Model that final crash
            // boundary without invoking ONI or Bridge.
            bool mutationSafetyLatch =
                File.Exists(confirmingPath) && confirming["action"]?.ToString() == "set_paused";
            confirming["status"] = "failed";
            confirming["result"] = new JObject { ["classification"] = "outcome_unknown" };
            string failedPath = Path.Combine(layout.Failed, Path.GetFileName(confirmingPath));
            File.Move(confirmingPath, failedPath);
            File.WriteAllText(
                failedPath,
                confirming.ToString(Formatting.Indented) + Environment.NewLine,
                new UTF8Encoding(false));
            Require(mutationSafetyLatch, "direct-verify restart did not latch mutation safety");
            Require(Load(failedPath)["result"]?["classification"]?.ToString() == "outcome_unknown", "direct-verify restart was not unknown");
            Passed.Add("crash_after_direct_verify_enters_confirming_then_restart_fails_unknown");
        }

        private static void RejectIntentTampering(string root)
        {
            Layout layout = CreateLayout(root, "cmd-intent-tamper");
            JObject claimed = Claim(layout);
            claimed["command_intent_hash"] =
                "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
            bool persisted = ExecutionClaimStore.TryPersistExecuting(
                layout.ExecutingPath,
                claimed,
                AttemptId,
                IntentHash,
                out string error);
            Require(!persisted && error.Contains("immutable intent hash"), "tampered intent hash was persisted");
            Passed.Add("immutable_intent_hash_tamper_is_rejected");
        }

        private static void CrashAfterBridgePrepareBeforeRename(string root)
        {
            Layout layout = CreateLayout(root, "cmd-after-bridge-prepare");
            JObject claimed = Claim(layout);
            RecordAcceptedAck(claimed, 71);
            claimed["executed_ack_seq"] = 72;
            claimed["local_effect_ack_seq"] = 73;
            claimed["status"] = "executed_pending_snapshot";
            Require(
                ExecutionClaimStore.TryTransitionExecuting(
                    layout.ExecutingPath,
                    layout.Confirming,
                    claimed,
                    AttemptId,
                    IntentHash,
                    out string confirmingPath,
                    out string transitionError),
                "could not enter confirming before Bridge prepare: " + transitionError);

            string confirmedAt = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture);
            JObject outcome = new JObject
            {
                ["schema"] = "oni.action-outcome.v1",
                ["command_id"] = claimed["id"].DeepClone(),
                ["action"] = claimed["action"].DeepClone(),
                ["command_intent_hash"] = IntentHash,
                ["command_location"] = "confirming",
                ["executor_status"] = "executed_pending_snapshot",
                ["confirmation_status"] = "snapshot_confirmed",
                ["classification"] = JValue.CreateNull(),
                ["blockers"] = new JArray(),
                ["source"] = new JObject(),
                ["direct"] = new JObject(),
                ["witness"] = new JObject { ["ts"] = confirmedAt },
                ["attestation_hash"] = JValue.CreateNull()
            };
            outcome["attestation_hash"] = HashWithoutField(outcome, "attestation_hash");

            JObject prepared = Load(confirmingPath);
            prepared["status"] = "confirmed";
            prepared["confirmed_at"] = confirmedAt;
            prepared["confirmation"] = outcome.DeepClone();
            JObject result = prepared["result"] as JObject ?? new JObject();
            result["attestation_hash"] = outcome["attestation_hash"].DeepClone();
            prepared["result"] = result;
            File.WriteAllText(
                confirmingPath,
                prepared.ToString(Formatting.Indented) + Environment.NewLine,
                new UTF8Encoding(false));

            JObject document = new JObject
            {
                ["schema"] = "oni.action-outcomes.v1",
                ["updated_at"] = confirmedAt,
                ["source"] = new JObject { ["session_id"] = "session harness/unsafe" },
                ["trust"] = new JObject(),
                ["counts"] = new JObject(),
                ["outcomes"] = new JArray(outcome.DeepClone()),
                ["outcomes_hash"] = JValue.CreateNull()
            };
            document["outcomes_hash"] = HashWithoutField(document, "outcomes_hash");
            WriteJson(Path.Combine(root, "action-outcomes.json"), document);
            string sessionDirectory = Path.Combine(root, "sessions", "session_harness_unsafe");
            Directory.CreateDirectory(sessionDirectory);
            WriteJson(Path.Combine(sessionDirectory, "action-outcomes.json"), document);

            JObject journal = new JObject
            {
                ["schema"] = PreparedConfirmationRecovery.JournalSchema,
                ["outcomes_hash"] = document["outcomes_hash"].DeepClone(),
                ["session_id"] = "session harness/unsafe",
                ["root_certificate"] = "action-outcomes.json",
                ["session_certificate"] = "sessions/session_harness_unsafe/action-outcomes.json",
                ["staged_at"] = confirmedAt,
                ["transaction_hash"] = JValue.CreateNull()
            };
            journal["transaction_hash"] = HashWithoutField(journal, "transaction_hash");
            WriteJson(Path.Combine(root, "action-outcomes-commit.json"), journal);

            Require(
                PreparedConfirmationRecovery.TryRecognize(
                    root,
                    prepared,
                    IntentHash,
                    out string recognitionError),
                "valid prepared confirmation was not recognized: " + recognitionError);
            string pendingWitnessId = prepared["id"].ToString();
            bool mutationLocked = !string.IsNullOrEmpty(pendingWitnessId);
            Require(mutationLocked, "prepared confirmation did not retain the mutation lock");
            Require(File.Exists(confirmingPath), "startup recognition destroyed the prepared confirmation");
            Require(!File.Exists(Path.Combine(layout.Done, Path.GetFileName(confirmingPath))), "startup recognition claimed success");

            string donePath = Path.Combine(layout.Done, Path.GetFileName(confirmingPath));
            File.Move(confirmingPath, donePath);
            JObject committed = Load(donePath);
            if (committed["confirmation"] is JObject committedConfirmation &&
                committed["status"]?.ToString() == "confirmed" &&
                committedConfirmation["confirmation_status"]?.ToString() == "snapshot_confirmed")
            {
                pendingWitnessId = null;
            }
            Require(string.IsNullOrEmpty(pendingWitnessId), "Bridge journal recovery did not release the mutation lock");
            Passed.Add("crash_after_bridge_prepare_preserves_confirming_until_journal_rename");
        }

        private static void BridgeRenameDuringStartupEnumeration(string root)
        {
            Layout layout = CreateLayout(root, "cmd-bridge-rename-race");
            string enumeratedPath = Path.Combine(layout.Confirming, "cmd-bridge-rename-race.json");
            File.Move(layout.ApprovedPath, enumeratedPath);
            string donePath = Path.Combine(layout.Done, Path.GetFileName(enumeratedPath));

            // Model Bridge's atomic move after Directory.GetFiles returned the
            // source name but before ActionExecutor opened it.
            File.Move(enumeratedPath, donePath);
            Require(
                PreparedConfirmationRecovery.WasRenamedToDone(
                    enumeratedPath,
                    layout.Done,
                    out string pendingWitnessId),
                "startup did not recognize Bridge winning the rename race");
            Require(
                pendingWitnessId == "cmd-bridge-rename-race",
                "rename-race witness id changed");
            Require(File.Exists(donePath), "rename-race recovery moved or deleted the committed target");
            Passed.Add("bridge_rename_during_startup_enumeration_retains_witness_validation_lock");
        }

        private static Layout CreateLayout(string root, string commandId)
        {
            string approved = Path.Combine(root, "approved");
            string executing = Path.Combine(root, "executing");
            string confirming = Path.Combine(root, "confirming");
            string failed = Path.Combine(root, "failed");
            string done = Path.Combine(root, "done");
            Directory.CreateDirectory(approved);
            Directory.CreateDirectory(executing);
            Directory.CreateDirectory(confirming);
            Directory.CreateDirectory(failed);
            Directory.CreateDirectory(done);
            string approvedPath = Path.Combine(approved, commandId + ".json");
            File.WriteAllText(
                approvedPath,
                new JObject
                {
                    ["schema"] = "oni.command.v3",
                    ["id"] = commandId,
                    ["status"] = "approved",
                    ["action"] = "set_paused",
                    ["session_id"] = "session harness/unsafe",
                    ["args"] = new JObject { ["paused"] = true }
                }.ToString(Formatting.Indented) + Environment.NewLine,
                new UTF8Encoding(false));
            return new Layout
            {
                ApprovedPath = approvedPath,
                ExecutingPath = Path.Combine(executing, commandId + ".json"),
                Executing = executing,
                Confirming = confirming,
                Failed = failed,
                Done = done
            };
        }

        private static JObject Claim(Layout layout)
        {
            JObject command = Load(layout.ApprovedPath);
            bool claimed = ExecutionClaimStore.TryClaimApproved(
                layout.ApprovedPath,
                layout.Executing,
                command,
                AttemptId,
                IntentHash,
                DateTimeOffset.UtcNow,
                out string executingPath,
                out string error);
            Require(claimed, "execution claim failed: " + error);
            Require(executingPath == layout.ExecutingPath, "claim target changed");
            Require(!File.Exists(layout.ApprovedPath), "approved command remained after claim");
            Require(File.Exists(layout.ExecutingPath), "executing command is missing after claim");
            JObject stored = Load(layout.ExecutingPath);
            Require(stored["attempt_id"]?.ToString() == AttemptId, "attempt id was not persisted");
            Require(stored["command_intent_hash"]?.ToString() == IntentHash, "intent hash was not persisted");
            RequireClaim(stored);
            return stored;
        }

        private static void RecordAcceptedAck(JObject command, long sequence)
        {
            command["accepted_ack_seq"] = sequence;
            command["execution_claim"]["accepted_ack"] = new JObject
            {
                ["state"] = "sequence_assigned",
                ["seq"] = sequence,
                ["durability"] = ExecutionClaimStore.AsyncAckDurability
            };
            command["execution_claim"]["dispatch"] = new JObject
            {
                ["state"] = "authorized_not_started"
            };
        }

        private static void Persist(Layout layout, JObject command)
        {
            Require(
                ExecutionClaimStore.TryPersistExecuting(
                    layout.ExecutingPath,
                    command,
                    AttemptId,
                    IntentHash,
                    out string error),
                "executing claim persistence failed: " + error);
        }

        private static void RecoverExecutingAsUnknown(Layout layout, JObject command)
        {
            bool mutationSafetyLatch = File.Exists(layout.ExecutingPath);
            Require(mutationSafetyLatch, "restart did not detect the surviving executing claim");
            command["status"] = "failed";
            command["result"] = new JObject
            {
                ["classification"] = "outcome_unknown",
                ["message"] = "Recovered surviving executing claim without replay."
            };
            bool transitioned = ExecutionClaimStore.TryTransitionExecuting(
                layout.ExecutingPath,
                layout.Failed,
                command,
                AttemptId,
                IntentHash,
                out string failedPath,
                out string error);
            Require(transitioned, "unknown recovery transition failed: " + error);
            Require(mutationSafetyLatch, "unknown recovery did not latch mutation safety");
            Require(!File.Exists(layout.ExecutingPath), "executing claim remained after unknown recovery");
            Require(Load(failedPath)["result"]?["classification"]?.ToString() == "outcome_unknown", "unknown classification was lost");
        }

        private static JObject RequireClaim(JObject command)
        {
            JObject claim = command["execution_claim"] as JObject;
            Require(claim != null, "execution_claim is missing");
            Require(claim["schema"]?.ToString() == ExecutionClaimStore.ClaimSchema, "claim schema changed");
            Require(claim["attempt_id"]?.ToString() == AttemptId, "claim attempt id changed");
            Require(claim["command_intent_hash"]?.ToString() == IntentHash, "claim intent hash changed");
            return claim;
        }

        private static JObject Load(string path)
        {
            return JObject.Parse(File.ReadAllText(path, Encoding.UTF8));
        }

        private static string HashWithoutField(JObject value, string field)
        {
            JObject payload = (JObject)value.DeepClone();
            payload.Remove(field);
            return CanonicalHashContract.Hash(payload);
        }

        private static void WriteJson(string path, JObject value)
        {
            File.WriteAllText(
                path,
                value.ToString(Formatting.Indented) + Environment.NewLine,
                new UTF8Encoding(false));
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }

        private sealed class Layout
        {
            internal string ApprovedPath;
            internal string ExecutingPath;
            internal string Executing;
            internal string Confirming;
            internal string Failed;
            internal string Done;
        }
    }
}
