using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Contracts;
using OniAiProbeMod.Probe;
using OniAiProbeMod.Security;
using UnityEngine;

namespace OniAiProbeMod.Commands
{
    internal static class ActionExecutor
    {
        private const float PollIntervalSeconds = 2f;
        private const float VerificationPollSeconds = 0.25f;
        // Code review 2026-09-26 (P2): the Bridge-confirmation witness was read
        // every frame, and a read failure re-latched, re-paused and re-logged
        // every frame.  Poll it on a cadence, tolerate a bounded run of
        // transient sharing violations, and resolve a failure exactly once.
        private const float WitnessPollSeconds = 0.5f;
        private const int MaxTransientWitnessFailures = 10;
        private const float VerificationTimeoutSeconds = 5f;
        private const int CommandClockSkewSeconds = 5;
        private const int MaxCommandIdLength = 80;
        private const int MaxFocusTagLength = 100;
        private const int MaxFocusMessageLength = 500;
        private const int MaxResearchIdLength = 128;
        private const int MaxDuplicantNameLength = 128;
        private const int MaxChoreGroupIdLength = 64;
        private const int MaxDigCells = 32;
        private const int MaxBuildingBlueprintCells = 8;
        private const int MaxUtilityBlueprintCells = 64;
        private const int MaxBlueprintPathIdLength = 64;
        private const int MaxMaterialTags = 8;
        private const int CancelEventHash = 2127324410;
        private const double FloatEqualityRelativeTolerance = 0.000001;
        private const string ApprovalReceiptSchema = "oni.approval-receipt.v1";
        private const string AutonomousApprovalReceiptSchema = "oni.approval-receipt.v2";
        private const string AutonomousCommandSchema = "oni.command.v4";
        private const string AutonomousPolicySchema = "oni.action-policy.v2";
        private const int MaxAuthorityIdentifierLength = 80;

        private static readonly string[] ApprovalReceiptFields =
        {
            "schema", "command_id", "command_intent_hash", "source_frame_id", "source",
            "policy_hash", "approved_by", "approved_at", "receipt_hash"
        };

        private static readonly string[] AutonomousPolicyRequiredFields =
        {
            "schema", "actions_enabled", "suggestions_enabled", "allowed_actions",
            "allowed_save_hashes", "command_ttl_seconds", "max_pending_commands",
            "execution_mode", "run_id", "controller_id", "session_id", "world_id"
        };

        private static readonly HashSet<string> PolicyActionCatalog = new HashSet<string>(
            new[]
            {
                "focus_note", "set_paused", "set_research", "set_duplicant_chore_priority",
                "mark_dig_cells", "cancel_dig_cells", "place_building_blueprint",
                "cancel_building_blueprint", "place_utility_blueprint_path",
                "cancel_utility_blueprint_path", "noop"
            },
            StringComparer.Ordinal);

        private static readonly string[] CommandIntentFields =
        {
            "schema",
            "id",
            "mode",
            "action",
            "args",
            "session_id",
            "save_id_hash",
            "world_id",
            "source_incident_id",
            "source_snapshot_id",
            "state_hash",
            "source_frame_id",
            "created_at",
            "expires_at",
            "risk",
            "reason",
            "evidence",
            "preconditions",
            "expected_effects",
            "requires_approval",
            "source"
        };

        private static readonly HashSet<string> InFlightIds = new HashSet<string>(StringComparer.Ordinal);
        private static readonly List<PendingVerification> PendingVerifications = new List<PendingVerification>();
        private static float nextPollAt;
        private static bool sessionStarted;
        private static bool mutationSafetyLatch;
        // True only while the latch comes solely from unresolved failed/
        // records found at session start; such a latch is re-evaluated when
        // failed/ changes, so an operator resolution takes effect without a
        // reload.  A latch raised during the session holds until reload.
        private static bool latchFromFailedRecordsOnly;
        private static string failedLatchSignature;
        private static string pendingWitnessId;
        private static float nextWitnessPollAt;
        private static int transientWitnessFailures;

        internal static void BeginSession()
        {
            FinalizeOutstandingAsUnknown("A new game session began before post-action verification completed.");
            InFlightIds.Clear();
            PendingVerifications.Clear();
            mutationSafetyLatch = false;
            latchFromFailedRecordsOnly = false;
            failedLatchSignature = null;
            pendingWitnessId = null;
            nextWitnessPollAt = 0f;
            transientWitnessFailures = 0;
            nextPollAt = 0f;
            sessionStarted = true;
            QuarantineStartupCommands();
            // A reload must not forget an unknown outcome (code review
            // 2026-09-26, P0-2): rebuild the durable latch from unresolved
            // failed/ records, exactly as agent/command_queue.py does.
            if (!mutationSafetyLatch)
            {
                LatchFromUnresolvedFailures(EnsureCommandDirectories());
            }
        }

        private static void SetSessionLatch()
        {
            mutationSafetyLatch = true;
            latchFromFailedRecordsOnly = false;
        }

        private static void LatchFromUnresolvedFailures(CommandDirectories dirs)
        {
            try
            {
                failedLatchSignature = OutcomeLatch.DirectorySignature(dirs.Failed);
                string unresolved = OutcomeLatch.FindUnresolvedFailure(dirs.Failed);
                if (unresolved == null)
                {
                    return;
                }
                mutationSafetyLatch = true;
                latchFromFailedRecordsOnly = true;
                ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                {
                    { "command_id", unresolved },
                    { "classification", "outcome_unknown" },
                    { "reason", "unresolved_failed_mutation_at_session_start" }
                });
            }
            catch (Exception ex)
            {
                SetSessionLatch();
                ProbeWriter.WriteError("command_failed_latch_scan", ex);
            }
        }

        private static void TryReleaseFailedRecordLatch(CommandDirectories dirs)
        {
            try
            {
                string signature = OutcomeLatch.DirectorySignature(dirs.Failed);
                if (string.Equals(signature, failedLatchSignature, StringComparison.Ordinal))
                {
                    return;
                }
                failedLatchSignature = signature;
                if (OutcomeLatch.FindUnresolvedFailure(dirs.Failed) != null)
                {
                    return;
                }
                mutationSafetyLatch = false;
                latchFromFailedRecordsOnly = false;
                ProbeWriter.WriteEvent("command_safety_latch_released", new Dictionary<string, object>
                {
                    { "reason", "failed_records_resolved" }
                });
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("command_failed_latch_scan", ex);
            }
        }

        internal static void EndSession()
        {
            sessionStarted = false;
            FinalizeOutstandingAsUnknown("The game session ended before post-action verification completed.");
            InFlightIds.Clear();
            PendingVerifications.Clear();
        }

        internal static void Update(float now)
        {
            if (!sessionStarted)
            {
                return;
            }

            VerifyPendingCommands(now);
            PollWitnessConfirmation(now);
            if (now < nextPollAt)
            {
                return;
            }

            nextPollAt = now + PollIntervalSeconds;
            PollApprovedCommands(now);
        }

        private static void QuarantineStartupCommands()
        {
            try
            {
                CommandDirectories dirs = EnsureCommandDirectories();
                foreach (string path in Directory.GetFiles(dirs.Approving, "*.json"))
                {
                    QuarantineFile(
                        path,
                        dirs,
                        "skipped_stale",
                        "approving_present_at_session_start",
                        "An interrupted approval claim predates this game session and was never dispatch-eligible.");
                }
                foreach (string path in Directory.GetFiles(dirs.Approved, "*.json"))
                {
                    QuarantineFile(
                        path,
                        dirs,
                        "skipped_stale",
                        "approved_present_at_session_start",
                        "Approved commands from an earlier game session are never replayed.");
                }
                foreach (string path in Directory.GetFiles(dirs.Executing, "*.json"))
                {
                    RecoverExecutingAtStartup(path, dirs);
                }
                foreach (string path in Directory.GetFiles(dirs.Confirming, "*.json"))
                {
                    string commandId = Path.GetFileNameWithoutExtension(path);
                    string action = "unknown";
                    string attemptId = null;
                    string commandIntentHash = null;
                    JObject command = null;
                    try
                    {
                        command = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                        commandId = ReadString(command, "id") ?? commandId;
                        action = ReadString(command, "action") ?? action;
                        attemptId = ReadString(command, "attempt_id");
                        commandIntentHash = ReadString(command, "command_intent_hash");
                    }
                    catch
                    {
                        if (PreparedConfirmationRecovery.WasRenamedToDone(
                                path,
                                dirs.Done,
                                out string concurrentlyCommittedId) &&
                            (string.IsNullOrEmpty(pendingWitnessId) ||
                             string.Equals(pendingWitnessId, concurrentlyCommittedId, StringComparison.Ordinal)))
                        {
                            // Bridge won the rename race after startup enumeration.
                            // PollWitnessConfirmation will validate the done payload
                            // before releasing this mutation lock.
                            pendingWitnessId = concurrentlyCommittedId;
                            ProbeWriter.WriteEvent("command_confirmation_recovery_waiting", new Dictionary<string, object>
                            {
                                { "command_id", concurrentlyCommittedId },
                                { "action", action },
                                { "classification", "bridge_rename_observed_pending_validation" },
                                { "success_claimed", false },
                                { "mutation_locked", true }
                            });
                            continue;
                        }
                        // A still-present unreadable confirming file is unknown.
                    }

                    // Legacy non-mutating commands never needed a later trusted
                    // game snapshot. Complete them without engaging the mutation
                    // latch so an old v2 noop/focus_note cannot block real actions.
                    if (command != null && !IsMutatingAction(action))
                    {
                        JObject existingResult = command["result"] as JObject;
                        long verifiedAckSeq = Ack(
                            commandId,
                            action,
                            "verified",
                            "Non-mutating command completed without snapshot confirmation.",
                            null,
                            attemptId,
                            commandIntentHash);
                        command["terminal_ack_seq"] = verifiedAckSeq;
                        FinalizeCommand(
                            path,
                            dirs.Done,
                            command,
                            "verified",
                            null,
                            "Non-mutating command completed without snapshot confirmation.",
                            existingResult?["before_state"] as JObject,
                            existingResult?["after_state"] as JObject);
                        continue;
                    }

                    // Bridge commits a trusted snapshot in three durable steps:
                    // publish root/session certificates and a journal, prepare
                    // this confirming payload in place, then rename it to done.
                    // A process crash between preparation and rename is not an
                    // unknown game mutation. Preserve the journal-bound payload
                    // and keep the mutation gate closed until Bridge recovery
                    // completes the rename; do not represent it as successful.
                    if (command != null && IsMutatingAction(action) &&
                        string.Equals(ReadString(command, "status"), "confirmed", StringComparison.Ordinal))
                    {
                        string preparedError = null;
                        string expectedIntentHash = null;
                        try
                        {
                            expectedIntentHash = CanonicalSha256(BuildCommandIntent(command));
                        }
                        catch (Exception ex)
                        {
                            preparedError = "Prepared command intent could not be hashed: " +
                                ex.GetBaseException().Message;
                        }

                        if (preparedError == null &&
                            PreparedConfirmationRecovery.TryRecognize(
                                ProbeWriter.BridgeDirectory,
                                command,
                                expectedIntentHash,
                                out preparedError))
                        {
                            if (string.IsNullOrEmpty(pendingWitnessId) ||
                                string.Equals(pendingWitnessId, commandId, StringComparison.Ordinal))
                            {
                                pendingWitnessId = commandId;
                                ProbeWriter.WriteEvent("command_confirmation_recovery_waiting", new Dictionary<string, object>
                                {
                                    { "command_id", commandId },
                                    { "action", action },
                                    { "classification", "prepared_confirmed_pending_bridge_commit" },
                                    { "success_claimed", false },
                                    { "mutation_locked", true }
                                });
                                continue;
                            }

                            // The executor normally permits only one such command.
                            // Preserve every valid prepared payload, but require an
                            // operator if an impossible second recovery is present.
                            SetSessionLatch();
                            ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                            {
                                { "command_id", commandId },
                                { "action", action },
                                { "classification", "outcome_unknown" },
                                { "reason", "multiple_prepared_confirmations_at_session_start" }
                            });
                            continue;
                        }

                        ProbeWriter.WriteEvent("command_confirmation_recovery_rejected", new Dictionary<string, object>
                        {
                            { "command_id", commandId },
                            { "action", action },
                            { "reason", preparedError ?? "prepared_confirmation_invalid" }
                        });
                    }

                    SetSessionLatch();
                    string confirmingReason =
                        "A prior mutating command lacked a same-session trusted snapshot confirmation." +
                        PauseForUnknownOutcome();
                    long failedAckSeq = Ack(
                        commandId,
                        action,
                        "failed",
                        confirmingReason,
                        "outcome_unknown",
                        attemptId,
                        commandIntentHash);
                    if (command != null)
                    {
                        command["terminal_ack_seq"] = failedAckSeq;
                        FinalizeCommand(
                            path,
                            dirs.Failed,
                            command,
                            "failed",
                            "outcome_unknown",
                            confirmingReason,
                            null,
                            null);
                    }
                    else
                    {
                        MoveRawFile(path, dirs.Failed);
                    }
                    ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                    {
                        { "command_id", commandId },
                        { "action", action },
                        { "classification", "outcome_unknown" },
                        { "reason", "confirming_present_at_session_start" }
                    });
                }
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("command_startup_quarantine", ex);
            }
        }

        private static void RecoverExecutingAtStartup(string path, CommandDirectories dirs)
        {
            string commandId = Path.GetFileNameWithoutExtension(path);
            string action = "unknown";
            string attemptId = null;
            string commandIntentHash = null;
            JObject command = null;
            try
            {
                command = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                commandId = ReadString(command, "id") ?? commandId;
                action = ReadString(command, "action") ?? action;
                attemptId = ReadString(command, "attempt_id");
                commandIntentHash = ReadString(command, "command_intent_hash");
            }
            catch
            {
                // Even a damaged executing claim proves that a prior process
                // crossed the durable claim boundary. Never infer no dispatch.
            }

            SetSessionLatch();
            string reason =
                "A durable executing claim survived process restart; whether its game mutation was dispatched is unknown." +
                PauseForUnknownOutcome();
            long failedAckSeq = Ack(
                commandId,
                action,
                "failed",
                reason,
                "outcome_unknown",
                attemptId,
                commandIntentHash);

            if (command != null)
            {
                command["terminal_ack_seq"] = failedAckSeq;
                if (!FinalizeCommand(
                    path,
                    dirs.Failed,
                    command,
                    "failed",
                    "outcome_unknown",
                    reason,
                    null,
                    null))
                {
                    // A malformed/tampered claim still must leave executing/.
                    // Preserve its raw bytes in failed/ when validated rewrite
                    // and transition cannot be completed.
                    MoveRawFile(path, dirs.Failed);
                }
            }
            else
            {
                MoveRawFile(path, dirs.Failed);
            }

            ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
            {
                { "command_id", commandId },
                { "action", action },
                { "classification", "outcome_unknown" },
                { "reason", "executing_present_at_session_start" },
                { "attempt_id", attemptId ?? string.Empty },
                { "command_intent_hash", commandIntentHash ?? string.Empty }
            });
        }

        private static void PollApprovedCommands(float now)
        {
            try
            {
                CommandDirectories dirs = EnsureCommandDirectories();
                string[] paths = Directory.GetFiles(dirs.Approved, "*.json");
                Array.Sort(paths, StringComparer.OrdinalIgnoreCase);
                foreach (string path in paths)
                {
                    string fileId = Path.GetFileNameWithoutExtension(path);
                    if (InFlightIds.Contains(fileId))
                    {
                        continue;
                    }

                    ExecuteFile(path, dirs, now);
                }
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("command_poll", ex);
            }
        }

        private static void ExecuteFile(string path, CommandDirectories dirs, float now)
        {
            string id = Path.GetFileNameWithoutExtension(path);
            string action = "unknown";
            string attemptId = null;
            string commandIntentHash = null;
            JObject command = null;
            ValidationResult validation = null;
            bool mutating = false;
            bool claimedForExecution = false;
            bool dispatchMayHaveStarted = false;
            try
            {
                command = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                id = ReadString(command, "id") ?? id;
                action = ReadString(command, "action") ?? "unknown";
                mutating = IsMutatingAction(action);

                // Keep one state-changing transaction in flight at a time.  A
                // deferred approved file remains untouched and is reconsidered
                // after the current command reaches a durable terminal state.
                if (mutating && mutationSafetyLatch && latchFromFailedRecordsOnly)
                {
                    TryReleaseFailedRecordLatch(dirs);
                }
                if (mutating &&
                    (mutationSafetyLatch ||
                     !string.IsNullOrEmpty(pendingWitnessId) ||
                     HasMutatingVerificationInFlight() ||
                     Directory.GetFiles(dirs.Executing, "*.json").Length > 0))
                {
                    return;
                }

                validation = ValidateCommand(command, path, dirs);
                if (!validation.IsValid)
                {
                    Ack(id, action, validation.AckStatus, validation.Reason, validation.Classification);
                    QuarantineCommand(path, dirs, command, validation.AckStatus, validation.Reason, validation.Classification);
                    return;
                }

                attemptId = "attempt-" + Guid.NewGuid().ToString("N");
                commandIntentHash = CanonicalSha256(BuildCommandIntent(command));
                if (mutating)
                {
                    if (!ExecutionClaimStore.TryClaimApproved(
                            path,
                            dirs.Executing,
                            command,
                            attemptId,
                            commandIntentHash,
                            DateTimeOffset.Now,
                            out string executingPath,
                            out string claimError))
                    {
                        string failure = "The approved command could not be durably claimed before dispatch: " + claimError;
                        Ack(id, action, "failed", failure, "runtime_fail", attemptId, commandIntentHash);
                        if (File.Exists(path))
                        {
                            QuarantineCommand(path, dirs, command, "failed", failure, "runtime_fail");
                        }
                        return;
                    }
                    path = executingPath;
                    claimedForExecution = true;
                    InFlightIds.Add(id);
                }
                else
                {
                    command["attempt_id"] = attemptId;
                    command["command_intent_hash"] = commandIntentHash;
                    command["execution_started_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
                }

                long acceptedAckSeq = Ack(
                    id,
                    action,
                    "accepted",
                    "Static policy and current-state preconditions passed.",
                    null,
                    attemptId,
                    commandIntentHash);
                if (acceptedAckSeq <= 0)
                {
                    string failure = "The accepted ACK could not be assigned a session sequence; no game API was dispatched.";
                    if (mutating)
                    {
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "runtime_fail",
                            failure,
                            validation.BeforeState,
                            null,
                            latchMutation: false);
                    }
                    else
                    {
                        QuarantineCommand(path, dirs, command, "rejected", failure, "runtime_fail");
                    }
                    return;
                }
                command["accepted_ack_seq"] = acceptedAckSeq;

                if (mutating)
                {
                    JObject claim = command["execution_claim"] as JObject;
                    claim["accepted_ack"] = new JObject
                    {
                        ["state"] = "sequence_assigned",
                        ["seq"] = acceptedAckSeq,
                        ["assigned_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture),
                        // WriteEvent only enqueues to ProbeWriter. Do not present
                        // this sequence assignment as a durable ACK record.
                        ["durability"] = ExecutionClaimStore.AsyncAckDurability
                    };
                    claim["dispatch"] = new JObject
                    {
                        ["state"] = "authorized_not_started",
                        ["authorized_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)
                    };
                    if (!PersistExecutionClaim(
                            path,
                            command,
                            attemptId,
                            commandIntentHash,
                            out string acceptedPersistenceError))
                    {
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "runtime_fail",
                            "The accepted ACK metadata could not be persisted before dispatch: " + acceptedPersistenceError,
                            validation.BeforeState,
                            null,
                            latchMutation: false);
                        return;
                    }
                }

                // Only the V4 route adds this final authorization read. A policy
                // or identity change while the durable claim/ACK was written
                // still has a known no-dispatch outcome, so it must not be
                // reported as an uncertain game mutation or retried.
                if (IsAutonomousCommand(command))
                {
                    string dispatchRefusal = ValidateAutonomousDispatch(
                        command, validation, dirs, path, commandIntentHash);
                    if (dispatchRefusal != null)
                    {
                        if (mutating)
                        {
                            FinalizeClaimedFailure(
                                path,
                                dirs,
                                command,
                                id,
                                action,
                                attemptId,
                                commandIntentHash,
                                "runtime_fail",
                                "Autonomous authorization was revoked before dispatch: " + dispatchRefusal,
                                validation.BeforeState,
                                null,
                                latchMutation: false);
                        }
                        else
                        {
                            QuarantineCommand(path, dirs, command, "rejected", dispatchRefusal, "runtime_fail");
                        }
                        return;
                    }
                }
                if (mutating)
                {
                    dispatchMayHaveStarted = true;
                }

                if (!ExecuteCommand(
                        command,
                        action,
                        out string executionMessage,
                        out List<GameObject> createdBlueprints))
                {
                    if (mutating)
                    {
                        RecordDispatchReturn(command, succeeded: false, executionMessage);
                        if (!PersistExecutionClaim(
                                path,
                                command,
                                attemptId,
                                commandIntentHash,
                                out string dispatchPersistenceError))
                        {
                            executionMessage += " Dispatch-return metadata persistence also failed: " + dispatchPersistenceError;
                        }
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "outcome_unknown",
                            executionMessage,
                            validation.BeforeState,
                            CaptureCurrentState(),
                            latchMutation: true);
                        return;
                    }

                    long failedAckSeq = Ack(
                        id,
                        action,
                        "failed",
                        executionMessage,
                        "behavior_fail",
                        attemptId,
                        commandIntentHash);
                    command["terminal_ack_seq"] = failedAckSeq;
                    FinalizeCommand(
                        path,
                        dirs.Failed,
                        command,
                        "failed",
                        "behavior_fail",
                        executionMessage,
                        validation.BeforeState,
                        CaptureCurrentState());
                    return;
                }

                if (mutating)
                {
                    RecordDispatchReturn(command, succeeded: true, executionMessage);
                    if (!PersistExecutionClaim(
                            path,
                            command,
                            attemptId,
                            commandIntentHash,
                            out string dispatchPersistenceError))
                    {
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "outcome_unknown",
                            "The mutation returned but its dispatch result could not be persisted: " + dispatchPersistenceError,
                            validation.BeforeState,
                            CaptureCurrentState(),
                            latchMutation: true);
                        return;
                    }
                }

                long executedAckSeq = Ack(
                    id,
                    action,
                    "executed",
                    executionMessage,
                    null,
                    attemptId,
                    commandIntentHash);
                command["executed_ack_seq"] = executedAckSeq;
                if (executedAckSeq <= acceptedAckSeq)
                {
                    string sequenceFailure = "The executed ACK did not advance the session sequence.";
                    if (mutating)
                    {
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "outcome_unknown",
                            sequenceFailure,
                            validation.BeforeState,
                            CaptureCurrentState(),
                            latchMutation: true);
                    }
                    else
                    {
                        FinalizeCommand(
                            path,
                            dirs.Failed,
                            command,
                            "failed",
                            "runtime_fail",
                            sequenceFailure,
                            validation.BeforeState,
                            CaptureCurrentState());
                    }
                    return;
                }

                if (mutating)
                {
                    JObject claim = command["execution_claim"] as JObject;
                    claim["executed_ack"] = new JObject
                    {
                        ["seq"] = executedAckSeq,
                        ["assigned_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture),
                        ["durability"] = ExecutionClaimStore.AsyncAckDurability
                    };
                    if (!PersistExecutionClaim(
                            path,
                            command,
                            attemptId,
                            commandIntentHash,
                            out string executedPersistenceError))
                    {
                        FinalizeClaimedFailure(
                            path,
                            dirs,
                            command,
                            id,
                            action,
                            attemptId,
                            commandIntentHash,
                            "outcome_unknown",
                            "The executed ACK metadata could not be persisted: " + executedPersistenceError,
                            validation.BeforeState,
                            CaptureCurrentState(),
                            latchMutation: true);
                        return;
                    }
                }
                else
                {
                    InFlightIds.Add(id);
                }
                PendingVerifications.Add(new PendingVerification
                {
                    Id = id,
                    Action = action,
                    Path = path,
                    Command = command,
                    BeforeState = validation.BeforeState,
                    ExecutionMessage = executionMessage,
                    CreatedBlueprints = createdBlueprints,
                    AcceptedPolicy = validation.AcceptedPolicy,
                    AcceptedSaveWriteGeneration = validation.AcceptedSaveWriteGeneration,
                    AttemptId = attemptId,
                    CommandIntentHash = commandIntentHash,
                    AcceptedAckSeq = acceptedAckSeq,
                    ExecutedAckSeq = executedAckSeq,
                    NextCheckAt = now,
                    Deadline = now + VerificationTimeoutSeconds
                });
            }
            catch (JsonException ex)
            {
                if (claimedForExecution && command != null)
                {
                    FinalizeClaimedFailure(
                        path,
                        dirs,
                        command,
                        id,
                        action,
                        attemptId,
                        commandIntentHash,
                        dispatchMayHaveStarted ? "outcome_unknown" : "runtime_fail",
                        "Executor JSON failure: " + ex.Message,
                        validation?.BeforeState,
                        null,
                        dispatchMayHaveStarted);
                    return;
                }
                Ack(id, action, "rejected", "Invalid command JSON: " + ex.Message, "behavior_fail");
                QuarantineFile(path, dirs, "rejected", "invalid_json", "Command JSON could not be parsed.");
            }
            catch (Exception ex)
            {
                if (claimedForExecution && command != null)
                {
                    FinalizeClaimedFailure(
                        path,
                        dirs,
                        command,
                        id,
                        action,
                        attemptId,
                        commandIntentHash,
                        dispatchMayHaveStarted ? "outcome_unknown" : "runtime_fail",
                        "Executor exception: " + ex.GetBaseException().Message,
                        validation?.BeforeState,
                        null,
                        dispatchMayHaveStarted);
                    return;
                }
                Ack(id, action, "failed", ex.GetBaseException().Message, "runtime_fail");
                QuarantineFile(path, dirs, "failed", "executor_exception", ex.GetBaseException().Message);
            }
        }

        private static ValidationResult ValidateCommand(JObject command, string path, CommandDirectories dirs)
        {
            string id = ReadString(command, "id");
            string action = ReadString(command, "action");
            string schema = ReadString(command, "schema");
            bool stateChanging = IsStateChangingAction(action);
            bool autonomous = IsAutonomousCommand(command);
            if (!autonomous && !string.Equals(schema, "oni.command.v3", StringComparison.Ordinal) &&
                !(string.Equals(schema, "oni.command.v2", StringComparison.Ordinal) && !stateChanging))
            {
                return ValidationResult.Reject(
                    stateChanging
                        ? "State-changing actions require oni.command.v3 or oni.command.v4 trusted field contracts."
                        : "Unsupported command schema.");
            }

            if (!IsSafeCommandId(id) || !string.Equals(Path.GetFileNameWithoutExtension(path), id, StringComparison.Ordinal))
            {
                return ValidationResult.Reject("Command id is invalid or does not match the file name.");
            }

            if (!string.Equals(ReadString(command, "status"), "approved", StringComparison.Ordinal))
            {
                return ValidationResult.Reject("An approved-directory command must have status=approved.");
            }

            if (command["requires_approval"]?.Type != JTokenType.Boolean || !command["requires_approval"].Value<bool>())
            {
                return ValidationResult.Reject("requires_approval must be true.");
            }

            if (!TryReadTimestamp(command, "approved_at", out DateTimeOffset approvedAt))
            {
                return ValidationResult.Reject("approved_at is required and must be a valid timestamp.");
            }

            DateTimeOffset currentTime = DateTimeOffset.Now;
            if (approvedAt > currentTime.AddMinutes(5))
            {
                return ValidationResult.Reject("approved_at is unreasonably far in the future.");
            }

            if (!TryReadTimestamp(command, "created_at", out DateTimeOffset createdAt) ||
                !TryReadTimestamp(command, "expires_at", out DateTimeOffset expiresAt))
            {
                return ValidationResult.Reject("created_at and expires_at are required timestamps.");
            }

            if (createdAt > currentTime.AddSeconds(CommandClockSkewSeconds))
            {
                return ValidationResult.Reject("created_at is unreasonably far in the future.");
            }

            if (approvedAt < createdAt || expiresAt <= createdAt)
            {
                return ValidationResult.Reject("Command timestamps are not in created/approved/expiry order.");
            }

            if (expiresAt <= currentTime || approvedAt > expiresAt)
            {
                return ValidationResult.Stale("Command approval or expiry is stale.");
            }

            if (HasProcessedId(dirs, id))
            {
                return ValidationResult.Stale("Command id was already finalized and cannot be replayed.");
            }

            JObject policy = LoadPolicy(out string policyError);
            if (policy == null)
            {
                return ValidationResult.Reject(policyError);
            }

            string authorityError = ValidateCommandAuthority(command, policy);
            if (authorityError != null)
            {
                return ValidationResult.Reject(authorityError);
            }

            if (policy["actions_enabled"]?.Type != JTokenType.Boolean || !policy["actions_enabled"].Value<bool>())
            {
                return ValidationResult.Reject("Actions are disabled by local policy.");
            }

            if (!PolicyContains(policy, "allowed_actions", action))
            {
                return ValidationResult.Reject("Action is not allowed by local policy: " + action);
            }

            string approvalReceiptError = stateChanging || autonomous ? ValidateApprovalReceipt(command, policy) : null;
            if (approvalReceiptError != null)
            {
                return ValidationResult.Reject(approvalReceiptError);
            }

            string argumentError = ValidateActionArguments(command, action);
            if (argumentError != null)
            {
                return ValidationResult.Reject(argumentError);
            }

            int policyTtl = ReadPositiveInt(policy, "command_ttl_seconds", 30);
            if (expiresAt - createdAt > TimeSpan.FromSeconds(policyTtl + 1))
            {
                return ValidationResult.Reject("Command TTL exceeds the local policy limit.");
            }

            string commandSession = ReadString(command, "session_id");
            if (!string.Equals(commandSession, ProbeSession.CurrentSessionId, StringComparison.Ordinal))
            {
                return ValidationResult.Stale("Command session_id does not match the active session.");
            }

            string sourceSnapshotId = ReadString(command, "source_snapshot_id");
            if (string.IsNullOrEmpty(sourceSnapshotId) ||
                !sourceSnapshotId.StartsWith(commandSession + ":", StringComparison.Ordinal))
            {
                return ValidationResult.Reject("source_snapshot_id is missing or belongs to another session.");
            }

            string stateHash = ReadString(command, "state_hash");
            if (string.IsNullOrEmpty(stateHash) || !stateHash.StartsWith("sha256:", StringComparison.Ordinal) || stateHash.Length != 71)
            {
                return ValidationResult.Reject("state_hash must be a SHA-256 audit hash.");
            }

            string activeSaveHash = ProbeSession.CurrentSaveIdHash;
            string commandSaveHash = ReadString(command, "save_id_hash");
            if (string.IsNullOrEmpty(activeSaveHash))
            {
                return ValidationResult.Stale("The active save identity is unavailable.");
            }

            if (ProbeSession.IsSaveDirty)
            {
                return ValidationResult.Stale("The active save session wrote a save after loading; actions are disabled.");
            }

            if (!string.Equals(commandSaveHash, activeSaveHash, StringComparison.Ordinal))
            {
                return ValidationResult.Stale("Command save_id_hash does not match the active save.");
            }

            if (!PolicyContains(policy, "allowed_save_hashes", activeSaveHash))
            {
                return ValidationResult.Reject("The active save is not in the action policy whitelist.");
            }

            if (command["world_id"]?.Type != JTokenType.Integer ||
                command["world_id"].Value<int>() != ProbeSession.CurrentWorldId)
            {
                return ValidationResult.Stale("Command world_id does not match the active world.");
            }

            string conditionShapeError = ValidateConditionShapes(command, action);
            if (conditionShapeError != null)
            {
                return ValidationResult.Reject(conditionShapeError);
            }

            JObject beforeState = CaptureCurrentState();
            if (!EvaluateConditions(beforeState, command["preconditions"] as JArray, out string preconditionError))
            {
                return ValidationResult.Reject("Current-state precondition failed: " + preconditionError);
            }

            // Refused here, before any claim or dispatch: a refusal at dispatch
            // would be classified outcome_unknown and latch the session.
            string unpauseRefusal = action == "set_paused" && !command["args"]["paused"].Value<bool>()
                ? DescribeUnpauseRefusal()
                : null;
            if (unpauseRefusal != null)
            {
                return ValidationResult.Stale("Unpause refused: " + unpauseRefusal + ".");
            }

            return ValidationResult.Valid(
                beforeState,
                (JObject)policy.DeepClone(),
                ProbeSession.SaveWriteGeneration);
        }

        private static bool IsAutonomousCommand(JObject command)
        {
            return string.Equals(ReadString(command, "schema"), AutonomousCommandSchema, StringComparison.Ordinal);
        }

        private static string ValidateCommandAuthority(JObject command, JObject policy)
        {
            bool autonomous = IsAutonomousCommand(command);
            string expectedPolicySchema = autonomous ? AutonomousPolicySchema : "oni.action-policy.v1";
            string expectedApprover = autonomous ? "autonomous_controller" : "local_human";
            if (!string.Equals(ReadString(policy, "schema"), expectedPolicySchema, StringComparison.Ordinal))
            {
                return "Command and action policy authority versions do not match.";
            }
            if (command["approved_by"]?.Type != JTokenType.String ||
                !string.Equals(ReadString(command, "approved_by"), expectedApprover, StringComparison.Ordinal))
            {
                return "approved_by does not identify the command's versioned authority path.";
            }

            if (!autonomous)
            {
                if (command["run_id"] != null || command["controller_id"] != null ||
                    policy["execution_mode"] != null || policy["run_id"] != null ||
                    policy["controller_id"] != null || policy["session_id"] != null || policy["world_id"] != null)
                {
                    return "Legacy human authority may not carry autonomous authority fields.";
                }
                // Non-mutating legacy commands do not require receipts, but a
                // supplied autonomous receipt must never select a mixed route.
                JToken receiptToken = command["approval_receipt"];
                if (receiptToken != null && receiptToken.Type != JTokenType.Null)
                {
                    JObject legacyReceipt = receiptToken as JObject;
                    if (legacyReceipt == null ||
                        !string.Equals(ReadString(legacyReceipt, "schema"), ApprovalReceiptSchema, StringComparison.Ordinal) ||
                        !string.Equals(ReadString(legacyReceipt, "approved_by"), "local_human", StringComparison.Ordinal) ||
                        legacyReceipt["run_id"] != null || legacyReceipt["controller_id"] != null)
                    {
                        return "Legacy commands require the V1 human receipt authority route.";
                    }
                }
                return null;
            }

            string policyError = ValidateAutonomousPolicy(policy);
            if (policyError != null)
            {
                return policyError;
            }
            string mode = ReadString(command, "mode");
            if (!string.Equals(mode, "model_selected", StringComparison.Ordinal) &&
                !string.Equals(mode, "suggested", StringComparison.Ordinal))
            {
                return "Autonomous V4 commands require mode=model_selected or mode=suggested.";
            }
            if (!IsAuthorityIdentifier(command["run_id"]) || !IsAuthorityIdentifier(command["controller_id"]) ||
                !JToken.DeepEquals(command["run_id"], policy["run_id"]) ||
                !JToken.DeepEquals(command["controller_id"], policy["controller_id"]))
            {
                return "Autonomous command run/controller authority does not exactly match the policy.";
            }
            if (!StartupBinding.IsSessionVerified || !ProbeSession.IsSaveSessionActive ||
                !string.Equals(ReadString(policy, "run_id"), StartupBinding.ValidationRunId, StringComparison.Ordinal))
            {
                return "Autonomous authority is not bound to the verified active startup run.";
            }
            if (command["session_id"]?.Type != JTokenType.String ||
                !JToken.DeepEquals(command["session_id"], policy["session_id"]) ||
                !string.Equals(ReadString(policy, "session_id"), ProbeSession.CurrentSessionId, StringComparison.Ordinal))
            {
                return "Autonomous authority does not match the active Probe session.";
            }
            if (!TryReadInt(command, "world_id", out int commandWorld) || commandWorld < 0 ||
                !JToken.DeepEquals(command["world_id"], policy["world_id"]) ||
                commandWorld != ProbeSession.CurrentWorldId)
            {
                return "Autonomous authority does not match the active world.";
            }
            JArray allowedSaves = policy["allowed_save_hashes"] as JArray;
            if (allowedSaves == null || allowedSaves.Count != 1 ||
                command["save_id_hash"]?.Type != JTokenType.String ||
                !JToken.DeepEquals(command["save_id_hash"], allowedSaves[0]) ||
                !string.Equals(ReadString(command, "save_id_hash"), ProbeSession.CurrentSaveIdHash, StringComparison.Ordinal))
            {
                return "Autonomous authority is not bound to the single active allowed save.";
            }
            return null;
        }

        private static string ValidateAutonomousPolicy(JObject policy)
        {
            if (AutonomousPolicyRequiredFields.Any(field => policy[field] == null) ||
                !HasOnlyKeys(policy, AutonomousPolicyRequiredFields.Concat(new[] { "generated_at" }).ToArray()))
            {
                return "Autonomous action policy has missing or unsupported fields.";
            }
            if (policy["execution_mode"]?.Type != JTokenType.String ||
                !string.Equals(ReadString(policy, "execution_mode"), "autonomous", StringComparison.Ordinal) ||
                !IsAuthorityIdentifier(policy["run_id"]) || !IsAuthorityIdentifier(policy["controller_id"]) ||
                !IsPolicySessionIdentifier(policy["session_id"]) ||
                !TryReadInt(policy, "world_id", out int worldId) || worldId < 0)
            {
                return "Autonomous action policy execution identity is invalid.";
            }
            if (policy["actions_enabled"]?.Type != JTokenType.Boolean ||
                policy["suggestions_enabled"]?.Type != JTokenType.Boolean ||
                !TryReadInt(policy, "command_ttl_seconds", out int ttl) || ttl < 1 || ttl > 300 ||
                !TryReadInt(policy, "max_pending_commands", out int maximumPending) || maximumPending < 1 || maximumPending > 100)
            {
                return "Autonomous action policy flags or command bounds are invalid.";
            }

            JArray allowedActions = policy["allowed_actions"] as JArray;
            HashSet<string> seenActions = new HashSet<string>(StringComparer.Ordinal);
            if (allowedActions == null || allowedActions.Count == 0)
            {
                return "Autonomous action policy allowed_actions is invalid.";
            }
            foreach (JToken action in allowedActions)
            {
                if (action.Type != JTokenType.String || !PolicyActionCatalog.Contains(action.ToString()) ||
                    !seenActions.Add(action.ToString()))
                {
                    return "Autonomous action policy allowed_actions is invalid.";
                }
            }

            JArray allowedSaves = policy["allowed_save_hashes"] as JArray;
            HashSet<string> seenSaves = new HashSet<string>(StringComparer.Ordinal);
            if (allowedSaves == null || allowedSaves.Count > 256)
            {
                return "Autonomous action policy allowed_save_hashes is invalid.";
            }
            foreach (JToken save in allowedSaves)
            {
                if (save.Type != JTokenType.String || !IsSha256Hash(save.ToString()) || !seenSaves.Add(save.ToString()))
                {
                    return "Autonomous action policy allowed_save_hashes is invalid.";
                }
            }
            if (policy["actions_enabled"].Value<bool>() && (allowedSaves.Count != 1 || maximumPending != 1))
            {
                return "Enabled autonomous actions require exactly one allowed save and max_pending_commands=1.";
            }
            if (policy["generated_at"] != null &&
                (policy["generated_at"].Type != JTokenType.String ||
                 !TryReadTimestamp(policy, "generated_at", out DateTimeOffset generatedAt) ||
                 !System.DateTime.TryParse(ReadString(policy, "generated_at"), CultureInfo.InvariantCulture,
                     DateTimeStyles.RoundtripKind, out System.DateTime generatedDate) || generatedDate.Kind == System.DateTimeKind.Unspecified))
            {
                return "Autonomous action policy generated_at requires a valid timestamp with an explicit offset.";
            }
            return null;
        }

        private static bool IsAuthorityIdentifier(JToken token)
        {
            if (token?.Type != JTokenType.String)
            {
                return false;
            }
            string value = token.ToString();
            if (value.Length == 0 || value.Length > MaxAuthorityIdentifierLength)
            {
                return false;
            }
            for (int index = 0; index < value.Length; index++)
            {
                char ch = value[index];
                bool asciiLetterOrDigit = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
                if (!asciiLetterOrDigit && (index == 0 || (ch != '.' && ch != '_' && ch != '-')))
                {
                    return false;
                }
            }
            return true;
        }

        private static bool IsPolicySessionIdentifier(JToken token)
        {
            if (token?.Type != JTokenType.String)
            {
                return false;
            }
            string value = token.ToString();
            if (value.Length == 0 || value.Length > 160)
            {
                return false;
            }
            foreach (char ch in value)
            {
                bool asciiLetterOrDigit = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
                if (!asciiLetterOrDigit && ch != '.' && ch != '_' && ch != ':' && ch != '@' && ch != '+' && ch != '-')
                {
                    return false;
                }
            }
            return true;
        }

        private static string ValidateApprovalReceipt(JObject command, JObject policy)
        {
            bool autonomous = IsAutonomousCommand(command);
            string authorityError = ValidateCommandAuthority(command, policy);
            if (authorityError != null)
            {
                return authorityError;
            }

            string[] receiptFields = autonomous
                ? ApprovalReceiptFields.Concat(new[] { "run_id", "controller_id" }).ToArray()
                : ApprovalReceiptFields;
            JObject receipt = command["approval_receipt"] as JObject;
            if (receipt == null || receiptFields.Any(field => receipt[field] == null) || !HasOnlyKeys(receipt, receiptFields))
            {
                return "approval_receipt is missing or contains unsupported fields.";
            }

            string expectedReceiptSchema = autonomous ? AutonomousApprovalReceiptSchema : ApprovalReceiptSchema;
            string expectedApprover = autonomous ? "autonomous_controller" : "local_human";
            if (!string.Equals(ReadString(receipt, "schema"), expectedReceiptSchema, StringComparison.Ordinal) ||
                !string.Equals(ReadString(receipt, "command_id"), ReadString(command, "id"), StringComparison.Ordinal) ||
                !string.Equals(ReadString(receipt, "approved_by"), expectedApprover, StringComparison.Ordinal) ||
                !string.Equals(ReadString(receipt, "approved_at"), ReadString(command, "approved_at"), StringComparison.Ordinal))
            {
                return "approval_receipt identity or approval fields do not match the command.";
            }
            if (autonomous &&
                (!JToken.DeepEquals(receipt["run_id"], command["run_id"]) ||
                 !JToken.DeepEquals(receipt["controller_id"], command["controller_id"])))
            {
                return "approval_receipt autonomous authority does not match the command.";
            }

            string sourceFrameId = ReadString(command, "source_frame_id");
            if (!IsSha256Hash(sourceFrameId) ||
                !string.Equals(ReadString(receipt, "source_frame_id"), sourceFrameId, StringComparison.Ordinal))
            {
                return "approval_receipt source_frame_id is invalid or mismatched.";
            }

            JObject receiptSource = receipt["source"] as JObject;
            if (receiptSource == null || !HasOnlyKeys(
                    receiptSource,
                    "session_id",
                    "save_id_hash",
                    "world_id",
                    "snapshot_id",
                    "state_hash"))
            {
                return "approval_receipt source binding is invalid.";
            }
            if (!JToken.DeepEquals(receiptSource["session_id"], command["session_id"]) ||
                !JToken.DeepEquals(receiptSource["save_id_hash"], command["save_id_hash"]) ||
                !JToken.DeepEquals(receiptSource["world_id"], command["world_id"]) ||
                !JToken.DeepEquals(receiptSource["snapshot_id"], command["source_snapshot_id"]) ||
                !JToken.DeepEquals(receiptSource["state_hash"], command["state_hash"]))
            {
                return "approval_receipt source binding does not match the command.";
            }

            string expectedIntentHash = CanonicalSha256(BuildCommandIntent(command));
            if (!string.Equals(
                    ReadString(receipt, "command_intent_hash"),
                    expectedIntentHash,
                    StringComparison.Ordinal))
            {
                return "approval_receipt command intent hash does not match the approved payload.";
            }

            string expectedPolicyHash = CanonicalSha256(policy);
            if (!string.Equals(ReadString(receipt, "policy_hash"), expectedPolicyHash, StringComparison.Ordinal))
            {
                return "The action policy changed after command authorization.";
            }
            JObject commandSource = command["source"] as JObject;
            if (commandSource == null ||
                !string.Equals(
                    ReadString(commandSource, "policy_hash"),
                    expectedPolicyHash,
                    StringComparison.Ordinal))
            {
                return "The command source does not bind the approved action policy.";
            }

            JObject receiptPayload = (JObject)receipt.DeepClone();
            receiptPayload.Remove("receipt_hash");
            string expectedReceiptHash = CanonicalSha256(receiptPayload);
            if (!string.Equals(ReadString(receipt, "receipt_hash"), expectedReceiptHash, StringComparison.Ordinal))
            {
                return "approval_receipt hash does not match its payload.";
            }

            return null;
        }

        private static JObject BuildCommandIntent(JObject command)
        {
            JObject intent = new JObject();
            foreach (string field in CommandIntentFields)
            {
                JToken value = command[field];
                intent[field] = value == null ? JValue.CreateNull() : value.DeepClone();
            }
            // Keep historical V3 hashes byte-for-byte compatible. Only V4 adds
            // these authority identifiers to the existing typed hash payload.
            if (IsAutonomousCommand(command))
            {
                intent["run_id"] = command["run_id"]?.DeepClone() ?? JValue.CreateNull();
                intent["controller_id"] = command["controller_id"]?.DeepClone() ?? JValue.CreateNull();
            }
            return intent;
        }

        private static string CanonicalSha256(JToken value)
        {
            return CanonicalHashContract.Hash(value);
        }

        private static bool IsSha256Hash(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length != 71 ||
                !value.StartsWith("sha256:", StringComparison.Ordinal))
            {
                return false;
            }
            for (int index = 7; index < value.Length; index++)
            {
                char current = value[index];
                if (!((current >= '0' && current <= '9') || (current >= 'a' && current <= 'f')))
                {
                    return false;
                }
            }
            return true;
        }

        private static string ValidateActionArguments(JObject command, string action)
        {
            JObject args = command["args"] as JObject;
            if (args == null)
            {
                return "args must be an object.";
            }

            switch (action)
            {
                case "noop":
                    return args.Count == 0 ? null : "noop does not accept arguments.";
                case "focus_note":
                {
                    if (!HasOnlyKeys(args, "tag", "message"))
                    {
                        return "focus_note contains an unsupported argument.";
                    }

                    string tag = ReadString(args, "tag") ?? string.Empty;
                    string message = ReadString(args, "message");
                    if (tag.Length > MaxFocusTagLength || string.IsNullOrWhiteSpace(message) || message.Length > MaxFocusMessageLength)
                    {
                        return "focus_note tag/message is missing or exceeds the local size limit.";
                    }

                    return null;
                }
                case "set_paused":
                    if (!HasOnlyKeys(args, "paused") || args["paused"]?.Type != JTokenType.Boolean)
                    {
                        return "set_paused requires exactly one boolean args.paused value.";
                    }

                    return null;
                case "set_research":
                {
                    string tech = ReadString(args, "tech");
                    if (!HasOnlyKeys(args, "tech") || string.IsNullOrWhiteSpace(tech) || tech.Length > MaxResearchIdLength)
                    {
                        return "set_research requires exactly one bounded string args.tech value.";
                    }

                    Tech techDefinition = Db.Get()?.Techs?.TryGet(tech);
                    if (techDefinition == null)
                    {
                        return "Unknown research tech id: " + tech;
                    }
                    // SetActiveResearch queues every unfinished prerequisite and
                    // activates the lowest tier, and clears the active research
                    // for a finished tech.  research.current can equal the target
                    // only when the target is unfinished and every prerequisite
                    // is complete; anything else is a deterministic mismatch that
                    // used to be classified outcome_unknown (review L11).
                    if (techDefinition.IsComplete())
                    {
                        return "set_research target is already researched: " + tech;
                    }
                    return techDefinition.ArePrerequisitesComplete()
                        ? null
                        : "set_research target has unfinished prerequisites: " + tech;
                }
                case "set_duplicant_chore_priority":
                {
                    if (!HasOnlyKeys(
                            args,
                            "duplicant_instance_id",
                            "expected_name",
                            "chore_group",
                            "from_priority",
                            "priority") ||
                        !TryReadInt(args, "duplicant_instance_id", out int instanceId) ||
                        instanceId <= 0 ||
                        !TryReadInt(args, "from_priority", out int fromPriority) ||
                        !TryReadInt(args, "priority", out int priority) ||
                        fromPriority < ChoreConsumer.MIN_PERSONAL_PRIORITY ||
                        fromPriority > ChoreConsumer.MAX_PERSONAL_PRIORITY ||
                        priority < ChoreConsumer.MIN_PERSONAL_PRIORITY ||
                        priority > ChoreConsumer.MAX_PERSONAL_PRIORITY ||
                        fromPriority == priority)
                    {
                        return "set_duplicant_chore_priority requires one positive instance id and different 0..5 from/target priorities.";
                    }

                    string expectedName = ReadString(args, "expected_name");
                    string groupId = ReadString(args, "chore_group");
                    if (string.IsNullOrWhiteSpace(expectedName) ||
                        expectedName.Length > MaxDuplicantNameLength ||
                        string.IsNullOrWhiteSpace(groupId) ||
                        groupId.Length > MaxChoreGroupIdLength ||
                        !groupId.All(char.IsLetterOrDigit))
                    {
                        return "set_duplicant_chore_priority requires a bounded expected_name and an alphanumeric chore_group id.";
                    }

                    return TryResolveDuplicantPriorityTarget(
                        instanceId,
                        expectedName,
                        groupId,
                        priority,
                        out _,
                        out _,
                        out string targetError)
                        ? null
                        : targetError;
                }
                case "mark_dig_cells":
                {
                    if (!HasOnlyKeys(args, "cells"))
                    {
                        return "mark_dig_cells requires exactly one cells argument.";
                    }

                    if (!TryReadUniqueCells(args["cells"], out List<int> cells, out string cellsError))
                    {
                        return "mark_dig_cells requires 1..32 unique non-negative cell ids: " + cellsError;
                    }

                    foreach (int cell in cells)
                    {
                        ProbeStateReader.TrackDigCell(cell);
                        if (!TryValidateDigMarkTarget(cell, out string targetError))
                        {
                            return targetError;
                        }
                    }

                    return null;
                }
                case "cancel_dig_cells":
                {
                    if (!HasOnlyKeys(args, "targets"))
                    {
                        return "cancel_dig_cells requires exactly one targets argument.";
                    }

                    if (!TryReadDigCancelTargets(args["targets"], out List<DigCancelTarget> targets, out string targetsError))
                    {
                        return "cancel_dig_cells requires 1..32 unique cell/designation instance pairs: " + targetsError;
                    }

                    foreach (DigCancelTarget target in targets)
                    {
                        ProbeStateReader.TrackDigCell(target.Cell);
                        if (!TryValidateDigCancelTarget(target, out _, out string targetError))
                        {
                            return targetError;
                        }
                    }

                    return null;
                }
                case "place_building_blueprint":
                {
                    if (!TryReadBuildingBlueprintDefId(args, out string buildingDefId, out string defError))
                    {
                        return "place_building_blueprint: " + defError;
                    }
                    if (!TryReadBlueprintPlacementArgs(
                            args,
                            buildingDefId,
                            MaxBuildingBlueprintCells,
                            requiresPathId: false,
                            out _,
                            out BuildingDef def,
                            out List<int> cells,
                            out List<Tag> materials,
                            out string placementError))
                    {
                        return "place_building_blueprint: " + placementError;
                    }
                    return TryValidateBlueprintPlacementTargets(def, cells, materials, out string targetError)
                        ? null
                        : targetError;
                }
                case "cancel_building_blueprint":
                {
                    string targetsError = "unsupported argument shape";
                    if (!HasOnlyKeys(args, "targets") ||
                        !TryReadBlueprintCancelTargets(
                            args["targets"],
                            MaxBuildingBlueprintCells,
                            out List<BlueprintCancelTarget> targets,
                            out targetsError))
                    {
                        return "cancel_building_blueprint requires 1..8 exact blueprint targets: " + targetsError;
                    }
                    foreach (BlueprintCancelTarget target in targets)
                    {
                        ProbeStateReader.TrackBlueprintCell(target.Cell);
                        if (!TryValidateBlueprintCancelTarget(
                                target,
                                null, // any allowlisted single building
                                expectedUtility: false,
                                out _,
                                out _,
                                out string targetError))
                        {
                            return targetError;
                        }
                    }
                    return null;
                }
                case "place_utility_blueprint_path":
                {
                    if (!TryReadUtilityBlueprintDefId(args, out string utilityDefId, out string utilityError))
                    {
                        return "place_utility_blueprint_path: " + utilityError;
                    }
                    if (!TryReadBlueprintPlacementArgs(
                            args,
                            utilityDefId,
                            MaxUtilityBlueprintCells,
                            requiresPathId: true,
                            out string pathId,
                            out BuildingDef def,
                            out List<int> cells,
                            out List<Tag> materials,
                            out string placementError))
                    {
                        return "place_utility_blueprint_path: " + placementError;
                    }
                    if (ProbeStateReader.UtilityPathExists(pathId))
                    {
                        return "Utility blueprint path_id already exists in this save session.";
                    }
                    return TryValidateBlueprintPlacementTargets(def, cells, materials, out string targetError)
                        ? null
                        : targetError;
                }
                case "cancel_utility_blueprint_path":
                {
                    string targetsError = "unsupported argument shape";
                    if (!HasOnlyKeys(args, "path_id", "targets") ||
                        !TryReadPathId(args, "path_id", out string pathId) ||
                        !TryReadBlueprintCancelTargets(
                            args["targets"],
                            MaxUtilityBlueprintCells,
                            out List<BlueprintCancelTarget> targets,
                            out targetsError))
                    {
                        return "cancel_utility_blueprint_path requires one safe path_id and 1..64 exact targets: " +
                            targetsError;
                    }
                    List<int> cells = targets.Select(target => target.Cell).ToList();
                    List<int> instanceIds = targets.Select(target => target.BlueprintInstanceId).ToList();
                    string utilityDefId = ProbeStateReader.UtilityPathDefId(pathId);
                    if (!ProbeStateReader.IsUtilityBlueprintDef(utilityDefId) ||
                        !ProbeStateReader.UtilityPathMatches(pathId, utilityDefId, cells, instanceIds))
                    {
                        return "Utility cancellation targets do not match the exact session path registry.";
                    }
                    foreach (BlueprintCancelTarget target in targets)
                    {
                        ProbeStateReader.TrackBlueprintCell(target.Cell);
                        if (!TryValidateBlueprintCancelTarget(
                                target,
                                utilityDefId,
                                expectedUtility: true,
                                out _,
                                out _,
                                out string targetError))
                        {
                            return targetError;
                        }
                    }
                    return null;
                }
                default:
                    return "Action is not whitelisted by the executor: " + action;
            }
        }

        private static string ValidateConditionShapes(JObject command, string action)
        {
            JArray preconditions = command["preconditions"] as JArray;
            JArray expectedEffects = command["expected_effects"] as JArray;
            if (preconditions == null || expectedEffects == null)
            {
                return "preconditions and expected_effects must both be arrays.";
            }

            if ((action == "set_paused" ||
                    action == "set_research" ||
                    action == "set_duplicant_chore_priority" ||
                    action == "mark_dig_cells" ||
                    action == "cancel_dig_cells" ||
                    action == "place_building_blueprint" ||
                    action == "cancel_building_blueprint" ||
                    action == "place_utility_blueprint_path" ||
                    action == "cancel_utility_blueprint_path") &&
                preconditions.Count == 0)
            {
                return "State-changing actions require at least one precondition.";
            }

            foreach (JToken token in preconditions.Concat(expectedEffects))
            {
                JObject condition = token as JObject;
                if (condition == null ||
                    string.IsNullOrWhiteSpace(ReadString(condition, "path")) ||
                    string.IsNullOrWhiteSpace(ReadString(condition, "op")) ||
                    condition["value"] == null)
                {
                    return "Every condition requires path, op, and value.";
                }

                string op = ReadString(condition, "op");
                if (op != "eq" && op != "ne" && op != "lt" && op != "lte" && op != "gt" && op != "gte" && op != "contains")
                {
                    return "Unsupported condition operator: " + op;
                }

                JToken quality = condition["quality"];
                if (quality == null ||
                    (quality.Type != JTokenType.String && quality.Type != JTokenType.Array))
                {
                    return "Every condition requires an explicit quality string or string array.";
                }

                if (IsStateChangingAction(action) && !IsObservedOnly(quality))
                {
                    return "State-changing action conditions require observed-only quality.";
                }

                if (!CommandFieldContracts.DeclarationMatches(condition, out string contractError))
                {
                    return contractError;
                }
                if (IsStateChangingAction(action) &&
                    !CommandFieldContracts.MutationEligible(condition, out string mutationContractError))
                {
                    return mutationContractError;
                }
            }

            if (action == "set_paused")
            {
                bool paused = command["args"]["paused"].Value<bool>();
                if (!ContainsObservedEquality(preconditions, "game.paused", new JValue(!paused)))
                {
                    return "set_paused must bind the observed opposite game.paused state.";
                }
                if (!ContainsExpectedEquality(expectedEffects, "game.paused", new JValue(paused)))
                {
                    return "set_paused must expect game.paused to equal args.paused.";
                }
                if (!paused)
                {
                    // Resuming is refused while a canonical alert is open, because
                    // an alert means something is already wrong.  Onset alerts are
                    // the exception: they mean a duplicant's current need is just
                    // starting to be unmet (breathable air, a toilet, rest), no
                    // duplicant is taking harm, and the need clears by itself once
                    // the game runs and the duplicant walks there.  Treating them as
                    // hard blockers deadlocked the autonomous loop: the game stayed
                    // paused, the duplicant could not move, the alert never cleared,
                    // and every resume was refused (2026-10-25 rounds 9-11).
                    // Genuine emergencies (suffocation, starvation, incapacitation,
                    // hypothermia/hyperthermia, sickness, ...) still refuse it.
                    if (!ContainsObservedEquality(preconditions, "alerts", new JArray())
                        && !ContainsOnlyOnsetAlerts(preconditions))
                    {
                        return "set_paused=false requires an observed empty canonical alert set, "
                            + "or one holding onset alerts only (" + string.Join(",", OnsetAlertIds) + ").";
                    }
                }
            }
            else if (action == "set_research")
            {
                string tech = command["args"]["tech"].ToString();
                if (!TryGetObservedEquality(preconditions, "research.current", out JToken currentResearch) ||
                    currentResearch.Type != JTokenType.String ||
                    string.Equals(currentResearch.ToString(), tech, StringComparison.Ordinal))
                {
                    return "set_research must bind a different observed research.current value.";
                }
                if (!ContainsExpectedEquality(expectedEffects, "research.current", new JValue(tech)))
                {
                    return "set_research must expect research.current to equal args.tech.";
                }
            }
            else if (action == "set_duplicant_chore_priority")
            {
                int instanceId = command["args"]["duplicant_instance_id"].Value<int>();
                int fromPriority = command["args"]["from_priority"].Value<int>();
                int priority = command["args"]["priority"].Value<int>();
                string groupId = command["args"]["chore_group"].ToString();
                string priorityPath = DuplicantPriorityPath(instanceId, groupId);
                if (!ContainsObservedEquality(preconditions, "game.paused", new JValue(true)))
                {
                    return "set_duplicant_chore_priority requires observed game.paused=true.";
                }

                if (!ContainsObservedEquality(preconditions, priorityPath, new JValue(fromPriority)))
                {
                    return "set_duplicant_chore_priority must bind the observed from_priority.";
                }

                if (!ContainsObservedEquality(expectedEffects, priorityPath, new JValue(priority)))
                {
                    return "set_duplicant_chore_priority must expect the observed target priority.";
                }
            }
            else if (action == "mark_dig_cells" || action == "cancel_dig_cells")
            {
                string digContractError = ValidateDigConditionContract(command, action, preconditions, expectedEffects);
                if (digContractError != null)
                {
                    return digContractError;
                }
            }
            else if (action == "place_building_blueprint" ||
                action == "cancel_building_blueprint" ||
                action == "place_utility_blueprint_path" ||
                action == "cancel_utility_blueprint_path")
            {
                string blueprintContractError = ValidateBlueprintConditionContract(
                    command,
                    action,
                    preconditions,
                    expectedEffects);
                if (blueprintContractError != null)
                {
                    return blueprintContractError;
                }
            }

            return null;
        }

        private static bool ExecuteCommand(
            JObject command,
            string action,
            out string message,
            out List<GameObject> createdBlueprints)
        {
            createdBlueprints = null;
            JObject args = (JObject)command["args"];
            switch (action)
            {
                case "noop":
                    message = "No-op action executed.";
                    return true;
                case "focus_note":
                    ProbeWriter.WriteEvent("focus_note", new Dictionary<string, object>
                    {
                        { "command_id", ReadString(command, "id") },
                        { "tag", ReadString(args, "tag") },
                        { "message", ReadString(args, "message") }
                    });
                    message = "Focus note recorded in the local probe log.";
                    return true;
                case "set_paused":
                    return TrySetPaused(args["paused"].Value<bool>(), out message);
                case "set_research":
                    return TrySetResearch(ReadString(args, "tech"), out message);
                case "set_duplicant_chore_priority":
                    return TrySetDuplicantChorePriority(args, out message);
                case "mark_dig_cells":
                    return TryMarkDigCells(args, out message);
                case "cancel_dig_cells":
                    return TryCancelDigCells(args, out message);
                case "place_building_blueprint":
                    return TryPlaceBlueprints(args, isUtilityPath: false, out createdBlueprints, out message);
                case "cancel_building_blueprint":
                    return TryCancelBlueprints(args, null, expectedUtility: false, out message);
                case "place_utility_blueprint_path":
                    return TryPlaceBlueprints(args, isUtilityPath: true, out createdBlueprints, out message);
                case "cancel_utility_blueprint_path":
                    // The utility comes from the session path registry.
                    return TryCancelBlueprints(args, null, expectedUtility: true, out message);
                default:
                    message = "Unsupported action.";
                    return false;
            }
        }

        // See PauseLedger: one Unpause() must release the game's only pause.
        private static string DescribeUnpauseRefusal()
        {
            // Managed null check only: IsPaused and the ledger are plain managed
            // state, and DestroyInstance clears the static on unload.
            SpeedControlScreen speedControl = SpeedControlScreen.Instance;
            if (ReferenceEquals(speedControl, null) || !speedControl.IsPaused)
            {
                // TrySetPaused reports the missing screen; an unpaused game
                // already matches and Unpause() is not called.
                return null;
            }

            bool countKnown = PauseLedger.TryGetCount(speedControl, out int pauseCount);
            // The scene scan runs only when the count alone allows the unpause.
            return PauseLedger.CountRefusal(PauseLedger.IsTracking, countKnown, pauseCount) ??
                PauseLedger.ModalRefusal(CountOpenPausingModals());
        }

        private static int CountOpenPausingModals()
        {
            int count = 0;
            // Only a shown modal holds a pause reference (PauseLedger.ModalHoldsPause);
            // KModalButtonMenu (the pause menu among them) always pauses while shown.
            foreach (KModalScreen screen in UnityEngine.Object.FindObjectsByType<KModalScreen>(FindObjectsSortMode.None))
            {
                if (screen != null && PauseLedger.ModalHoldsPause(screen))
                {
                    count++;
                }
            }
            foreach (KModalButtonMenu menu in UnityEngine.Object.FindObjectsByType<KModalButtonMenu>(FindObjectsSortMode.None))
            {
                if (menu != null && PauseLedger.ModalHoldsPause(menu))
                {
                    count++;
                }
            }
            return count;
        }

        private static bool TrySetPaused(bool paused, out string message)
        {
            SpeedControlScreen speedControl = SpeedControlScreen.Instance;
            if (speedControl == null)
            {
                message = "SpeedControlScreen.Instance is unavailable.";
                return false;
            }

            if (speedControl.IsPaused == paused)
            {
                message = "Pause state already matches the requested value.";
                return true;
            }

            if (paused)
            {
                speedControl.Pause(false, false);
            }
            else
            {
                speedControl.Unpause(false);
            }

            message = "Pause API call completed; awaiting state verification.";
            return true;
        }

        private static bool TrySetResearch(string tech, out string message)
        {
            Research research = Research.Instance;
            Tech definition = Db.Get()?.Techs?.TryGet(tech);
            if (research == null || definition == null)
            {
                message = "Research API or requested tech is unavailable.";
                return false;
            }

            // ONI only enqueues the requested tech when the queue is empty. Clear the
            // existing queue so this action's promised "set active" semantics are exact.
            research.SetActiveResearch(definition, true);
            message = "Research queue replaced with " + tech + "; awaiting state verification.";
            return true;
        }

        private static bool TrySetDuplicantChorePriority(JObject args, out string message)
        {
            int instanceId = args["duplicant_instance_id"].Value<int>();
            int fromPriority = args["from_priority"].Value<int>();
            int priority = args["priority"].Value<int>();
            string expectedName = ReadString(args, "expected_name");
            string groupId = ReadString(args, "chore_group");
            if (!TryResolveDuplicantPriorityTarget(
                    instanceId,
                    expectedName,
                    groupId,
                    priority,
                    out ChoreConsumer consumer,
                    out ChoreGroup group,
                    out message))
            {
                return false;
            }

            int current = consumer.GetPersonalPriority(group);
            if (current != fromPriority)
            {
                message = "Duplicant chore priority changed after validation; expected " +
                    fromPriority.ToString(CultureInfo.InvariantCulture) + " but found " +
                    current.ToString(CultureInfo.InvariantCulture) + ".";
                return false;
            }

            consumer.SetPersonalPriority(group, priority);
            message = "Duplicant chore priority API call completed for " + expectedName + "/" + groupId +
                "; awaiting state verification.";
            return true;
        }

        private static bool TryResolveDuplicantPriorityTarget(
            int instanceId,
            string expectedName,
            string groupId,
            int targetPriority,
            out ChoreConsumer consumer,
            out ChoreGroup group,
            out string error)
        {
            consumer = null;
            group = null;
            if (ProbeSession.CurrentWorldId < 0 || Db.Get()?.ChoreGroups == null)
            {
                error = "Current world or chore group database is unavailable.";
                return false;
            }

            foreach (ChoreGroup candidate in Db.Get().ChoreGroups.resources)
            {
                if (candidate != null && string.Equals(candidate.Id, groupId, StringComparison.Ordinal))
                {
                    group = candidate;
                    break;
                }
            }

            if (group == null || !group.userPrioritizable)
            {
                error = "Unknown or non-user-prioritizable chore group: " + groupId;
                return false;
            }

            IEnumerable<MinionIdentity> identities = Components.LiveMinionIdentities.GetWorldItems(
                ProbeSession.CurrentWorldId,
                checkChildWorlds: false);
            if (identities == null)
            {
                error = "Live duplicant registry is unavailable for the current world.";
                return false;
            }

            foreach (MinionIdentity identity in identities)
            {
                KPrefabID prefab = identity?.GetComponent<KPrefabID>();
                if (prefab == null || prefab.InstanceID != instanceId)
                {
                    continue;
                }

                if (!string.Equals(identity.GetProperName(), expectedName, StringComparison.Ordinal))
                {
                    error = "Duplicant instance id matched but expected_name did not.";
                    return false;
                }

                consumer = identity.GetComponent<ChoreConsumer>();
                if (consumer == null)
                {
                    error = "Target duplicant has no ChoreConsumer.";
                    return false;
                }

                if (targetPriority > ChoreConsumer.PRIORITY_DISABLED && !consumer.IsPermittedByTraits(group))
                {
                    error = "Target duplicant traits permanently disable chore group: " + groupId;
                    return false;
                }

                error = null;
                return true;
            }

            error = "Target duplicant instance id is not live in the current world.";
            return false;
        }

        private static bool TryMarkDigCells(JObject args, out string message)
        {
            if (!TryReadUniqueCells(args["cells"], out List<int> cells, out message))
            {
                return false;
            }

            foreach (int cell in cells)
            {
                ProbeStateReader.TrackDigCell(cell);
                if (!TryValidateDigMarkTarget(cell, out message))
                {
                    return false;
                }
            }

            List<GameObject> created = new List<GameObject>();
            foreach (int cell in cells)
            {
                GameObject gameObject = DigTool.PlaceDig(cell, created.Count);
                if (gameObject == null)
                {
                    CancelCreatedDigDesignations(created);
                    message = "DigTool.PlaceDig returned no designation; previously created items were cancelled.";
                    return false;
                }

                created.Add(gameObject);
                if (gameObject.GetComponent<Diggable>() == null)
                {
                    CancelCreatedDigDesignations(created);
                    message = "DigTool.PlaceDig returned an object without Diggable; every returned item was cancelled.";
                    return false;
                }
            }

            message = "Created " + created.Count.ToString(CultureInfo.InvariantCulture) +
                " tile-only dig designation(s); awaiting state verification.";
            return true;
        }

        private static bool TryCancelDigCells(JObject args, out string message)
        {
            if (!TryReadDigCancelTargets(args["targets"], out List<DigCancelTarget> targets, out message))
            {
                return false;
            }

            List<Diggable> designations = new List<Diggable>();
            foreach (DigCancelTarget target in targets)
            {
                ProbeStateReader.TrackDigCell(target.Cell);
                if (!TryValidateDigCancelTarget(target, out Diggable designation, out message))
                {
                    return false;
                }

                designations.Add(designation);
            }

            foreach (Diggable designation in designations)
            {
                designation.gameObject.Trigger(CancelEventHash);
            }

            message = "Triggered cancellation for " + designations.Count.ToString(CultureInfo.InvariantCulture) +
                " exact dig designation(s); awaiting asynchronous state verification.";
            return true;
        }

        private static void CancelCreatedDigDesignations(IEnumerable<GameObject> created)
        {
            foreach (GameObject gameObject in created)
            {
                if (gameObject != null)
                {
                    gameObject.Trigger(CancelEventHash, BoxedBools.True);
                }
            }
        }

        private static bool TryPlaceBlueprints(
            JObject args,
            bool isUtilityPath,
            out List<GameObject> created,
            out string message)
        {
            created = new List<GameObject>();
            string expectedDefId;
            if (isUtilityPath
                ? !TryReadUtilityBlueprintDefId(args, out expectedDefId, out message)
                : !TryReadBuildingBlueprintDefId(args, out expectedDefId, out message))
            {
                return false;
            }
            int maximum = isUtilityPath ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
            if (!TryReadBlueprintPlacementArgs(
                    args,
                    expectedDefId,
                    maximum,
                    isUtilityPath,
                    out string pathId,
                    out BuildingDef def,
                    out List<int> cells,
                    out List<Tag> materials,
                    out message) ||
                !TryValidateBlueprintPlacementTargets(def, cells, materials, out message))
            {
                return false;
            }
            if (isUtilityPath && ProbeStateReader.UtilityPathExists(pathId))
            {
                message = "Utility blueprint path_id already exists in this save session.";
                return false;
            }

            IUtilityNetworkMgr utilityManager = null;
            if (isUtilityPath)
            {
                IHaveUtilityNetworkMgr networkProvider = def.BuildingComplete.GetComponent<IHaveUtilityNetworkMgr>();
                utilityManager = networkProvider?.GetNetworkManager();
                if (utilityManager == null)
                {
                    message = expectedDefId + " utility network manager is unavailable.";
                    return false;
                }
            }

            foreach (int cell in cells)
            {
                Vector3 position = Grid.CellToPosCBC(cell, def.SceneLayer);
                GameObject blueprint = def.TryPlace(
                    null,
                    position,
                    Orientation.Neutral,
                    materials,
                    "DEFAULT_FACADE",
                    restrictToActiveWorld: true);
                if (blueprint == null)
                {
                    int rolledBack = CancelCreatedBlueprints(created);
                    message = "BuildingDef.TryPlace returned no blueprint; rolled back " +
                        rolledBack.ToString(CultureInfo.InvariantCulture) + " previously created item(s).";
                    return false;
                }

                created.Add(blueprint);
                if (blueprint.GetComponent<BuildingComplete>() != null ||
                    blueprint.GetComponent<Constructable>() == null ||
                    Grid.PosToCell(blueprint) != cell)
                {
                    int rolledBack = CancelCreatedBlueprints(created);
                    message = "BuildingDef.TryPlace returned an unsafe or mismatched object; rolled back " +
                        rolledBack.ToString(CultureInfo.InvariantCulture) + " item(s).";
                    return false;
                }
                if (isUtilityPath && blueprint.GetComponent<KAnimGraphTileVisualizer>() == null)
                {
                    int rolledBack = CancelCreatedBlueprints(created);
                    message = expectedDefId + " blueprint lacks its utility visualizer; rolled back " +
                        rolledBack.ToString(CultureInfo.InvariantCulture) + " item(s).";
                    return false;
                }
            }

            if (isUtilityPath)
            {
                // Register the path's own adjacency before reading each cell's
                // connections, exactly as WireBuildTool.ApplyPathToConduitSystem
                // does.  Without it every blueprint kept Connections=0 and the
                // built path became isolated single-cell networks (code review
                // 2026-09-26, G1).  All blueprints exist at this point, so a
                // later cancellation clears these bits through the visualizer.
                for (int index = 1; index < cells.Count; index++)
                {
                    UtilityConnections direction = UtilityConnectionsExtensions.DirectionFromToCell(
                        cells[index - 1],
                        cells[index]);
                    if (direction == 0)
                    {
                        int rolledBack = CancelCreatedBlueprints(created);
                        message = expectedDefId + " path cells are not four-neighbor adjacent; rolled back " +
                            rolledBack.ToString(CultureInfo.InvariantCulture) + " item(s).";
                        return false;
                    }
                    utilityManager.AddConnection(direction, cells[index - 1], is_physical_building: false);
                    utilityManager.AddConnection(direction.InverseDirection(), cells[index], is_physical_building: false);
                }
                for (int index = 0; index < cells.Count; index++)
                {
                    UtilityConnections connections = utilityManager.GetConnections(
                        cells[index],
                        is_physical_building: false);
                    IUtilityItem utilityItem = created[index].GetComponent<KAnimGraphTileVisualizer>();
                    utilityItem.Connections = connections;
                    TileVisualizer.RefreshCell(cells[index], def.TileLayer, def.ReplacementLayer);
                }
                ProbeStateReader.RegisterUtilityPath(pathId, def.PrefabID, cells, created);
            }

            message = "Created " + created.Count.ToString(CultureInfo.InvariantCulture) + " " +
                expectedDefId + " blueprint(s) through BuildingDef.TryPlace; awaiting state verification.";
            return true;
        }

        private static bool TryCancelBlueprints(
            JObject args,
            string expectedDefId,
            bool expectedUtility,
            out string message)
        {
            int maximum = expectedUtility ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
            if (!TryValidateBlueprintEnvironment(out message) ||
                !TryReadBlueprintCancelTargets(
                    args["targets"],
                    maximum,
                    out List<BlueprintCancelTarget> targets,
                    out message))
            {
                return false;
            }
            if (expectedUtility)
            {
                if (!TryReadPathId(args, "path_id", out string pathId))
                {
                    message = "Utility cancellation path_id is invalid.";
                    return false;
                }
                List<int> cells = targets.Select(target => target.Cell).ToList();
                List<int> instanceIds = targets.Select(target => target.BlueprintInstanceId).ToList();
                expectedDefId = ProbeStateReader.UtilityPathDefId(pathId);
                if (!ProbeStateReader.IsUtilityBlueprintDef(expectedDefId) ||
                    !ProbeStateReader.UtilityPathMatches(pathId, expectedDefId, cells, instanceIds))
                {
                    message = "Utility cancellation targets no longer match the exact session path registry.";
                    return false;
                }
            }

            List<GameObject> blueprints = new List<GameObject>();
            foreach (BlueprintCancelTarget target in targets)
            {
                ProbeStateReader.TrackBlueprintCell(target.Cell);
                if (!TryValidateBlueprintCancelTarget(
                        target,
                        expectedDefId,
                        expectedUtility,
                        out GameObject blueprint,
                        out _,
                        out message))
                {
                    return false;
                }
                blueprints.Add(blueprint);
            }
            foreach (GameObject blueprint in blueprints)
            {
                blueprint.Trigger(CancelEventHash);
            }
            message = "Triggered cancellation for " + blueprints.Count.ToString(CultureInfo.InvariantCulture) +
                " exact unfinished " + expectedDefId + " blueprint(s); awaiting asynchronous state verification.";
            return true;
        }

        private static int CancelCreatedBlueprints(IEnumerable<GameObject> created)
        {
            int cancelled = 0;
            foreach (GameObject blueprint in created)
            {
                if (blueprint == null)
                {
                    continue;
                }
                Constructable constructable = blueprint.GetComponent<Constructable>();
                if (constructable == null ||
                    blueprint.GetComponent<BuildingComplete>() != null ||
                    !TryReadBlueprintConstructionState(constructable, out bool inProgress, out _)
                    || inProgress)
                {
                    continue;
                }
                blueprint.Trigger(CancelEventHash, BoxedBools.True);
                cancelled++;
            }
            return cancelled;
        }

        private static bool TryValidateBlueprintEnvironment(out string error)
        {
            if (SpeedControlScreen.Instance == null || !SpeedControlScreen.Instance.IsPaused)
            {
                error = "Blueprint actions require the game to remain paused.";
                return false;
            }
            if (Game.Instance == null || Game.Instance.SandboxModeActive || DebugHandler.InstantBuildMode)
            {
                error = "Blueprint actions are disabled in Sandbox or Instant Build mode.";
                return false;
            }
            if (ProbeSession.CurrentWorldId < 0 || ClusterManager.Instance == null)
            {
                error = "Current world is unavailable for blueprint actions.";
                return false;
            }
            error = null;
            return true;
        }

        private static bool TryValidateBlueprintPlacementTargets(
            BuildingDef def,
            IList<int> cells,
            IList<Tag> materials,
            out string error)
        {
            if (!TryValidateBlueprintEnvironment(out error))
            {
                return false;
            }
            WorldContainer world = ClusterManager.Instance.GetWorld(ProbeSession.CurrentWorldId);
            if (def == null || world == null || !def.Cancellable ||
                !ProbeStateReader.ValidateBlueprintMaterials(def, materials))
            {
                error = "Blueprint definition, cancellation support, world, or selected materials are invalid.";
                return false;
            }
            foreach (int cell in cells)
            {
                ProbeStateReader.TrackBlueprintCell(cell);
                if (!ProbeStateReader.IsSafeBlueprintPlacement(
                        cell,
                        ProbeSession.CurrentWorldId,
                        def,
                        materials))
                {
                    error = "Blueprint cell is no longer an empty valid placement target: " +
                        cell.ToString(CultureInfo.InvariantCulture);
                    return false;
                }
            }
            error = null;
            return true;
        }

        private static bool TryValidateBlueprintCancelTarget(
            BlueprintCancelTarget target,
            string expectedDefId,
            bool expectedUtility,
            out GameObject blueprint,
            out Constructable constructable,
            out string error)
        {
            blueprint = null;
            constructable = null;
            if (!TryValidateBlueprintEnvironment(out error) ||
                !Grid.IsValidCellInWorld(target.Cell, ProbeSession.CurrentWorldId) ||
                !ProbeStateReader.TryFindBlueprint(
                    target.Cell,
                    target.BlueprintInstanceId,
                    out blueprint,
                    out constructable,
                    out Building building))
            {
                error = error ?? "Blueprint cancellation target no longer matches the exact instance and cell.";
                return false;
            }
            if (building.Def == null ||
                !(expectedDefId == null
                    ? IsAllowlistedBuildingBlueprint(building.Def.PrefabID)
                    : string.Equals(building.Def.PrefabID, expectedDefId, StringComparison.Ordinal)) ||
                building.Def.isUtility != expectedUtility ||
                !building.Def.Cancellable ||
                building.Orientation != Orientation.Neutral ||
                blueprint.GetComponent<BuildingComplete>() != null)
            {
                error = "Blueprint cancellation target has an unexpected definition, orientation, or completion state.";
                blueprint = null;
                constructable = null;
                return false;
            }
            if (!TryReadBlueprintConstructionState(constructable, out bool inProgress, out string constructionError))
            {
                error = constructionError ?? "Blueprint construction state is unavailable; cancellation is refused.";
                blueprint = null;
                constructable = null;
                return false;
            }
            if (inProgress)
            {
                error = "Blueprint construction is already in progress and cannot be cancelled automatically.";
                blueprint = null;
                constructable = null;
                return false;
            }
            error = null;
            return true;
        }

        private static bool TryReadBlueprintConstructionState(
            Constructable constructable,
            out bool inProgress,
            out string error)
        {
            inProgress = false;
            error = null;
            if (constructable == null)
            {
                error = "Blueprint construction component is unavailable.";
                return false;
            }
            try
            {
                object worker = constructable.GetWorker();
                float progress = constructable.GetPercentComplete();
                // ONI uses -1 as the native not-started sentinel. Any other
                // non-finite or out-of-range value is unknown and fail-closed.
                if (float.IsNaN(progress) || float.IsInfinity(progress) ||
                    progress > 1f || (progress < 0f && progress != -1f))
                {
                    error = "Blueprint construction progress is unknown; mutation is refused.";
                    return false;
                }
                inProgress = worker != null || progress > 0f;
                return true;
            }
            catch
            {
                error = "Blueprint construction state read failed; mutation is refused.";
                return false;
            }
        }

        private static bool TryValidateDigMarkTarget(int cell, out string error)
        {
            int worldId = ProbeSession.CurrentWorldId;
            if (worldId < 0 || !Grid.IsValidCellInWorld(cell, worldId))
            {
                error = "Dig cell is invalid or outside the current world: " + cell.ToString(CultureInfo.InvariantCulture);
                return false;
            }

            if (DigTool.Instance == null ||
                !DigTool.Instance.IsActiveLayer(ToolParameterMenu.FILTERLAYERS.TILES) ||
                DigTool.Instance.IsActiveLayer(ToolParameterMenu.FILTERLAYERS.NATURALBACKWALL))
            {
                error = "DigTool must be available with Tiles on and NaturalBackwall off.";
                return false;
            }

            if (!ProbeStateReader.IsSafeDigMarkCandidate(cell, worldId))
            {
                error = "Dig cell is not a visible natural solid candidate or already has a designation/constructable: " +
                    cell.ToString(CultureInfo.InvariantCulture);
                return false;
            }

            error = null;
            return true;
        }

        private static bool TryValidateDigCancelTarget(
            DigCancelTarget target,
            out Diggable designation,
            out string error)
        {
            designation = null;
            int worldId = ProbeSession.CurrentWorldId;
            if (worldId < 0 || !Grid.IsValidCellInWorld(target.Cell, worldId))
            {
                error = "Dig cancellation cell is invalid or outside the current world: " +
                    target.Cell.ToString(CultureInfo.InvariantCulture);
                return false;
            }

            designation = Diggable.GetDiggable(target.Cell);
            KPrefabID prefab = designation?.GetComponent<KPrefabID>();
            if (designation == null || prefab == null || prefab.InstanceID != target.DesignationInstanceId)
            {
                error = "Dig cancellation target no longer matches the exact designation instance.";
                designation = null;
                return false;
            }

            if (designation.digTypeFlags != (int)Diggable.DiggableType.Tile)
            {
                error = "Only tile-only dig designations may be cancelled by batch B.";
                designation = null;
                return false;
            }

            if (designation.chore != null && designation.chore.InProgress())
            {
                error = "Dig designation is already in progress and cannot be cancelled automatically.";
                designation = null;
                return false;
            }

            error = null;
            return true;
        }

        private static bool IsMutatingAction(string action)
        {
            return !string.Equals(action, "noop", StringComparison.Ordinal) &&
                !string.Equals(action, "focus_note", StringComparison.Ordinal);
        }

        private static string ValidateAutonomousDispatch(
            JObject command,
            ValidationResult accepted,
            CommandDirectories dirs,
            string claimedPath,
            string commandIntentHash)
        {
            if (accepted == null || accepted.AcceptedPolicy == null ||
                !string.Equals(CanonicalSha256(BuildCommandIntent(command)), commandIntentHash, StringComparison.Ordinal))
            {
                return "The accepted autonomous intent is unavailable or changed.";
            }
            JObject policy = LoadPolicy(out string policyError);
            if (policy == null)
            {
                return "The autonomous action policy is unavailable: " + policyError;
            }
            if (!JToken.DeepEquals(accepted.AcceptedPolicy, policy))
            {
                return "The autonomous action policy changed after the execution claim.";
            }
            string authorityError = ValidateApprovalReceipt(command, policy);
            if (authorityError != null)
            {
                return authorityError;
            }
            if (policy["actions_enabled"]?.Type != JTokenType.Boolean || !policy["actions_enabled"].Value<bool>())
            {
                return "Autonomous actions are disabled by local policy.";
            }
            if (ProbeSession.IsSaveDirty || ProbeSession.SaveWriteGeneration != accepted.AcceptedSaveWriteGeneration)
            {
                return "The active save or save-write generation changed before dispatch.";
            }
            if (!TryReadTimestamp(command, "expires_at", out DateTimeOffset expiresAt) || expiresAt <= DateTimeOffset.Now)
            {
                return "Autonomous command authorization expired before dispatch.";
            }

            if (IsMutatingAction(ReadString(command, "action")))
            {
                if (mutationSafetyLatch || !string.IsNullOrEmpty(pendingWitnessId) ||
                    HasMutatingVerificationInFlight() || OutcomeLatch.FindUnresolvedFailure(dirs.Failed) != null ||
                    Directory.GetFiles(dirs.Confirming, "*.json").Length != 0)
                {
                    return "An unresolved outcome or another mutation blocks autonomous dispatch.";
                }
                string[] executingPaths = Directory.GetFiles(dirs.Executing, "*.json");
                if (executingPaths.Length != 1 ||
                    !string.Equals(Path.GetFullPath(executingPaths[0]), Path.GetFullPath(claimedPath), StringComparison.OrdinalIgnoreCase))
                {
                    return "The autonomous command is not the sole durable executing claim.";
                }
                JObject durableClaim = ParseObject(File.ReadAllText(claimedPath, Encoding.UTF8));
                string claimError = ExecutionClaimStore.ValidateClaim(
                    durableClaim, ReadString(command, "attempt_id"), commandIntentHash);
                if (claimError != null || !JToken.DeepEquals(durableClaim, command))
                {
                    return "The durable autonomous execution claim changed before dispatch: " + (claimError ?? "payload mismatch");
                }
            }

            JObject currentState = CaptureCurrentState();
            if (!EvaluateConditions(currentState, command["preconditions"] as JArray, out string preconditionError))
            {
                return "Current-state autonomous precondition failed before dispatch: " + preconditionError;
            }
            string unpauseRefusal = ReadString(command, "action") == "set_paused" && !command["args"]["paused"].Value<bool>()
                ? DescribeUnpauseRefusal()
                : null;
            if (unpauseRefusal != null)
            {
                return "Unpause refused before autonomous dispatch: " + unpauseRefusal + ".";
            }
            // Bind the before-state evidence to this final read, rather than to
            // the earlier read preceding durable claim persistence.
            accepted.BeforeState = currentState;
            return null;
        }

        private static bool PersistExecutionClaim(
            string path,
            JObject command,
            string attemptId,
            string commandIntentHash,
            out string error)
        {
            string currentIntentHash = CanonicalSha256(BuildCommandIntent(command));
            if (!string.Equals(currentIntentHash, commandIntentHash, StringComparison.Ordinal))
            {
                error = "The immutable command intent changed after the execution claim.";
                return false;
            }
            return ExecutionClaimStore.TryPersistExecuting(
                path,
                command,
                attemptId,
                commandIntentHash,
                out error);
        }

        private static void RecordDispatchReturn(JObject command, bool succeeded, string message)
        {
            JObject claim = command?["execution_claim"] as JObject;
            if (claim == null)
            {
                return;
            }
            claim["dispatch"] = new JObject
            {
                ["state"] = "returned",
                ["returned_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture),
                ["reported_success"] = succeeded,
                ["message"] = message
            };
        }

        private static void FinalizeClaimedFailure(
            string path,
            CommandDirectories dirs,
            JObject command,
            string id,
            string action,
            string attemptId,
            string commandIntentHash,
            string classification,
            string message,
            JObject beforeState,
            JObject afterState,
            bool latchMutation)
        {
            bool outcomeUnknown = string.Equals(classification, "outcome_unknown", StringComparison.Ordinal);
            if (latchMutation || outcomeUnknown)
            {
                SetSessionLatch();
                message += PauseForUnknownOutcome();
            }

            JObject claim = command?["execution_claim"] as JObject;
            if (claim != null)
            {
                claim["terminal"] = new JObject
                {
                    ["classification"] = classification,
                    ["recorded_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)
                };
            }
            long failedAckSeq = Ack(
                id,
                action,
                "failed",
                message,
                classification,
                attemptId,
                commandIntentHash);
            command["terminal_ack_seq"] = failedAckSeq;
            bool finalized = FinalizeCommand(
                path,
                dirs.Failed,
                command,
                "failed",
                classification,
                message,
                beforeState,
                afterState);
            InFlightIds.Remove(id);

            if (!finalized)
            {
                // A retained executing claim blocks every later mutation. Its
                // dispatch boundary cannot be reconstructed by another poll.
                MoveRawFile(path, dirs.Failed);
                SetSessionLatch();
                message = "The executing claim could not reach failed/." + PauseForUnknownOutcome();
                outcomeUnknown = true;
            }
            if (outcomeUnknown)
            {
                ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                {
                    { "command_id", id },
                    { "action", action },
                    { "classification", "outcome_unknown" },
                    { "reason", message },
                    { "attempt_id", attemptId ?? string.Empty },
                    { "command_intent_hash", commandIntentHash ?? string.Empty }
                });
            }
        }

        private static bool HasMutatingVerificationInFlight()
        {
            foreach (PendingVerification pending in PendingVerifications)
            {
                if (pending != null && IsMutatingAction(pending.Action))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool VerificationContextStillValid(PendingVerification pending, out string error)
        {
            JObject command = pending?.Command;
            if (command == null)
            {
                error = "The in-flight command payload is unavailable.";
                return false;
            }

            if (!string.Equals(
                    ReadString(command, "session_id"),
                    ProbeSession.CurrentSessionId,
                    StringComparison.Ordinal))
            {
                error = "The active session changed during post-action verification.";
                return false;
            }

            if (ProbeSession.IsSaveDirty)
            {
                error = "A save write is active or could not be verified during post-action verification.";
                return false;
            }

            if (ProbeSession.SaveWriteGeneration != pending.AcceptedSaveWriteGeneration)
            {
                error = "The save-write generation changed during post-action verification.";
                return false;
            }

            if (!string.Equals(
                    ReadString(command, "save_id_hash"),
                    ProbeSession.CurrentSaveIdHash,
                    StringComparison.Ordinal))
            {
                error = "The active save identity changed during post-action verification.";
                return false;
            }

            if (command["world_id"]?.Type != JTokenType.Integer ||
                command["world_id"].Value<int>() != ProbeSession.CurrentWorldId)
            {
                error = "The active world changed during post-action verification.";
                return false;
            }

            JObject policy = LoadPolicy(out string policyError);
            if (policy == null)
            {
                error = "The action policy became unavailable during post-action verification: " + policyError;
                return false;
            }

            if (pending.AcceptedPolicy == null || !JToken.DeepEquals(pending.AcceptedPolicy, policy))
            {
                error = "The action policy changed during post-action verification.";
                return false;
            }

            string authorityError = ValidateCommandAuthority(command, policy);
            if (authorityError != null)
            {
                error = "The accepted authority no longer matches this verification context: " + authorityError;
                return false;
            }
            if (IsAutonomousCommand(command) &&
                (!string.Equals(CanonicalSha256(BuildCommandIntent(command)), pending.CommandIntentHash, StringComparison.Ordinal) ||
                 ValidateApprovalReceipt(command, policy) != null))
            {
                error = "The autonomous intent or approval receipt changed during post-action verification.";
                return false;
            }
            if (policy["actions_enabled"]?.Type != JTokenType.Boolean ||
                !policy["actions_enabled"].Value<bool>() ||
                !PolicyContains(policy, "allowed_actions", pending.Action) ||
                !PolicyContains(policy, "allowed_save_hashes", ProbeSession.CurrentSaveIdHash))
            {
                error = "The accepted action policy no longer authorizes this verification context.";
                return false;
            }

            error = null;
            return true;
        }

        private static void FinalizeOutstandingAsUnknown(string reason)
        {
            for (int index = PendingVerifications.Count - 1; index >= 0; index--)
            {
                PendingVerification pending = PendingVerifications[index];
                FinishPendingUnknown(index, pending, null, reason);
            }
        }

        private static void PollWitnessConfirmation(float now)
        {
            if (string.IsNullOrEmpty(pendingWitnessId))
            {
                transientWitnessFailures = 0;
                return;
            }
            if (now < nextWitnessPollAt)
            {
                return;
            }
            nextWitnessPollAt = now + WitnessPollSeconds;

            try
            {
                CommandDirectories dirs = EnsureCommandDirectories();
                string donePath = Path.Combine(dirs.Done, pendingWitnessId + ".json");
                if (File.Exists(donePath))
                {
                    JObject command = ParseObject(File.ReadAllText(donePath, Encoding.UTF8));
                    JObject confirmation = command["confirmation"] as JObject;
                    if (string.Equals(ReadString(command, "id"), pendingWitnessId, StringComparison.Ordinal) &&
                        string.Equals(ReadString(command, "status"), "confirmed", StringComparison.Ordinal) &&
                        confirmation != null &&
                        string.Equals(
                            ReadString(confirmation, "confirmation_status"),
                            "snapshot_confirmed",
                            StringComparison.Ordinal) &&
                        string.Equals(
                            ReadString(confirmation, "command_id"),
                            pendingWitnessId,
                            StringComparison.Ordinal))
                    {
                        pendingWitnessId = null;
                        return;
                    }

                    SetSessionLatch();
                    string invalidReason = "Bridge terminal confirmation is malformed or mismatched.";
                    PauseForUnknownOutcome();
                    ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                    {
                        { "command_id", pendingWitnessId },
                        { "classification", "outcome_unknown" },
                        { "reason", invalidReason }
                    });
                    pendingWitnessId = null;
                    return;
                }

                bool confirmingExists = Directory.GetFiles(dirs.Confirming, "*.json")
                    .Any(path => string.Equals(
                        Path.GetFileNameWithoutExtension(path),
                        pendingWitnessId,
                        StringComparison.Ordinal));
                transientWitnessFailures = 0;
                if (!confirmingExists)
                {
                    // The Bridge may have resolved this command outside
                    // ``confirming``: when it can decide the outcome itself (the
                    // declared effect never appeared) it moves the record to
                    // ``rejected`` with its decision embedded.  That is a
                    // resolution, not a disappearance.  Without this check the
                    // Bridge's own terminal decision latched the executor for the
                    // rest of the session, so every later command expired
                    // unexecuted (2026-10-26 round 39).
                    if (TryAcceptBridgeTerminalDecision(dirs, pendingWitnessId))
                    {
                        pendingWitnessId = null;
                        return;
                    }
                    SetSessionLatch();
                    string missingReason = "The executed command disappeared before trusted snapshot confirmation.";
                    PauseForUnknownOutcome();
                    ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                    {
                        { "command_id", pendingWitnessId },
                        { "classification", "outcome_unknown" },
                        { "reason", missingReason }
                    });
                    pendingWitnessId = null;
                }
            }
            catch (Exception ex)
            {
                // A reader such as a copy tool or the Bridge can hold the file
                // briefly; retry on the next polls before calling it unknown.
                if ((ex is IOException || ex is UnauthorizedAccessException) &&
                    ++transientWitnessFailures <= MaxTransientWitnessFailures)
                {
                    return;
                }

                SetSessionLatch();
                PauseForUnknownOutcome();
                ProbeWriter.WriteError("command_witness_poll", ex);
                ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                {
                    { "command_id", pendingWitnessId },
                    { "classification", "outcome_unknown" },
                    { "reason", "The Bridge terminal confirmation could not be read." }
                });
                // Resolved once: the latch now blocks mutations, so the witness
                // must not re-pause and re-log every poll.
                pendingWitnessId = null;
                transientWitnessFailures = 0;
            }
        }

        /// <summary>
        /// True when the Bridge recorded a terminal decision for this command.
        ///
        /// A confirmation the Bridge can decide itself (the declared effect never
        /// appeared) moves the record from ``confirming`` to ``rejected`` with the
        /// decision embedded.  The executor must read that as a resolution rather
        /// than as a command that disappeared, otherwise the Bridge's own decision
        /// latches the mutation safety latch and no later command can ever run
        /// (2026-10-26 round 39).
        /// </summary>
        private static bool TryAcceptBridgeTerminalDecision(CommandDirectories dirs, string commandId)
        {
            if (string.IsNullOrEmpty(commandId))
            {
                return false;
            }

            string[] stages = { dirs.Rejected, dirs.Done };
            foreach (string stage in stages)
            {
                string path = Path.Combine(stage, commandId + ".json");
                if (!File.Exists(path))
                {
                    continue;
                }
                try
                {
                    JObject record = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                    JObject confirmation = record["confirmation"] as JObject;
                    if (confirmation == null)
                    {
                        continue;
                    }
                    if (!string.Equals(
                            ReadString(confirmation, "command_id"),
                            commandId,
                            StringComparison.Ordinal))
                    {
                        continue;
                    }
                    string status = ReadString(confirmation, "confirmation_status");
                    if (!string.Equals(status, "rejected", StringComparison.Ordinal) &&
                        !string.Equals(status, "snapshot_confirmed", StringComparison.Ordinal))
                    {
                        continue;
                    }
                    ProbeWriter.WriteEvent("command_terminal_decision_accepted", new Dictionary<string, object>
                    {
                        { "command_id", commandId },
                        { "confirmation_status", status },
                        { "stage", Path.GetFileName(stage) }
                    });
                    return true;
                }
                catch (Exception ex)
                {
                    ProbeWriter.WriteError("command_terminal_decision_read", ex);
                }
            }
            return false;
        }

        private static void VerifyPendingCommands(float now)
        {
            for (int index = PendingVerifications.Count - 1; index >= 0; index--)
            {
                PendingVerification pending = PendingVerifications[index];
                if (now < pending.NextCheckAt)
                {
                    continue;
                }

                pending.NextCheckAt = now + VerificationPollSeconds;
                if (!VerificationContextStillValid(pending, out string contextError))
                {
                    JObject uncertainState = null;
                    try
                    {
                        uncertainState = CaptureCurrentState();
                    }
                    catch
                    {
                        // The missing direct read is part of the unknown outcome.
                    }
                    FinishPendingUnknown(index, pending, uncertainState, contextError);
                    continue;
                }

                JObject afterState;
                try
                {
                    afterState = CaptureCurrentState();
                }
                catch (Exception ex)
                {
                    if (now < pending.Deadline)
                    {
                        continue;
                    }

                    FinishPendingUnknown(
                        index,
                        pending,
                        null,
                        "Post-action state read failed after dispatch: " + ex.GetBaseException().Message);
                    continue;
                }

                bool expected = EvaluateConditions(afterState, pending.Command["expected_effects"] as JArray, out string conditionError);
                bool actionVerified = VerifyActionSpecific(afterState, pending.Command, pending.Action, out string actionError);
                if (expected && actionVerified)
                {
                    bool mutating = IsMutatingAction(pending.Action);
                    string directReadMessage = mutating
                        ? "Executor direct-read matches every expected effect; awaiting a later trusted snapshot."
                        : "Executor direct-read completed the non-mutating command; no game snapshot confirmation is required.";
                    long localEffectAckSeq = Ack(
                        pending.Id,
                        pending.Action,
                        "local_effect_observed",
                        directReadMessage,
                        null,
                        pending.AttemptId,
                        pending.CommandIntentHash);
                    pending.Command["local_effect_ack_seq"] = localEffectAckSeq;
                    if (localEffectAckSeq <= pending.ExecutedAckSeq)
                    {
                        FinishPendingUnknown(
                            index,
                            pending,
                            afterState,
                            "The local-effect ACK did not advance the executed ACK sequence.");
                        continue;
                    }

                    if (mutating)
                    {
                        JObject claim = pending.Command["execution_claim"] as JObject;
                        if (claim == null)
                        {
                            FinishPendingUnknown(
                                index,
                                pending,
                                afterState,
                                "The durable execution claim metadata disappeared before direct verification commit.");
                            continue;
                        }
                        claim["direct_verification"] = new JObject
                        {
                            ["state"] = "local_effect_observed",
                            ["ack_seq"] = localEffectAckSeq,
                            ["ack_durability"] = ExecutionClaimStore.AsyncAckDurability,
                            ["observed_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)
                        };
                    }
                    CommandDirectories dirs = EnsureCommandDirectories();
                    bool committed = FinalizeCommand(
                        pending.Path,
                        mutating ? dirs.Confirming : dirs.Done,
                        pending.Command,
                        mutating ? "executed_pending_snapshot" : "verified",
                        null,
                        pending.ExecutionMessage,
                        pending.BeforeState,
                        afterState);
                    if (!committed)
                    {
                        FinishPendingUnknown(
                            index,
                            pending,
                            afterState,
                            mutating
                                ? "The executed command could not be durably committed to confirming/."
                                : "The non-mutating command could not be committed to done/.");
                        continue;
                    }
                    InFlightIds.Remove(pending.Id);
                    PendingVerifications.RemoveAt(index);
                    if (mutating)
                    {
                        pendingWitnessId = pending.Id;
                    }
                    continue;
                }

                if (now >= pending.Deadline)
                {
                    string reason = !expected ? conditionError : actionError;
                    FinishPendingUnknown(
                        index,
                        pending,
                        afterState,
                        "Post-action verification timed out after dispatch: " + reason);
                }
            }
        }

        private static void FinishPendingFailure(int index, PendingVerification pending, JObject afterState, string reason)
        {
            if (pending.CreatedBlueprints != null && pending.CreatedBlueprints.Count > 0)
            {
                try
                {
                    int rolledBack = CancelCreatedBlueprints(pending.CreatedBlueprints);
                    reason += " Automatic blueprint rollback triggered for " +
                        rolledBack.ToString(CultureInfo.InvariantCulture) + "/" +
                        pending.CreatedBlueprints.Count.ToString(CultureInfo.InvariantCulture) + " created item(s).";
                }
                catch (Exception ex)
                {
                    reason += " Automatic blueprint rollback raised: " + ex.GetBaseException().Message;
                }
            }
            long failedAckSeq = Ack(
                pending.Id,
                pending.Action,
                "failed",
                reason,
                "behavior_fail",
                pending.AttemptId,
                pending.CommandIntentHash);
            pending.Command["terminal_ack_seq"] = failedAckSeq;
            FinalizeCommand(
                pending.Path,
                EnsureCommandDirectories().Failed,
                pending.Command,
                "failed",
                "behavior_fail",
                reason,
                pending.BeforeState,
                afterState);
            InFlightIds.Remove(pending.Id);
            PendingVerifications.RemoveAt(index);
        }

        private static void FinishPendingUnknown(
            int index,
            PendingVerification pending,
            JObject afterState,
            string reason)
        {
            bool mutating = pending != null && IsMutatingAction(pending.Action);
            string safetyAction = "";
            if (mutating)
            {
                SetSessionLatch();
                safetyAction = PauseForUnknownOutcome();
            }

            string message = "Post-action outcome is unknown and no further state-changing command will run in this session: " +
                reason + safetyAction;
            string id = pending?.Id ?? "unknown";
            string action = pending?.Action ?? "unknown";
            long failedAckSeq = Ack(
                id,
                action,
                "failed",
                message,
                "outcome_unknown",
                pending?.AttemptId,
                pending?.CommandIntentHash);
            if (pending != null)
            {
                pending.Command["terminal_ack_seq"] = failedAckSeq;
                FinalizeCommand(
                    pending.Path,
                    EnsureCommandDirectories().Failed,
                    pending.Command,
                    "failed",
                    "outcome_unknown",
                    message,
                    pending.BeforeState,
                    afterState);
                InFlightIds.Remove(pending.Id);
            }

            if (index >= 0 && index < PendingVerifications.Count)
            {
                PendingVerifications.RemoveAt(index);
            }

            if (mutating)
            {
                ProbeWriter.WriteEvent("command_safety_latched", new Dictionary<string, object>
                {
                    { "command_id", id },
                    { "action", action },
                    { "classification", "outcome_unknown" },
                    { "reason", reason }
                });
            }
        }

        private static string PauseForUnknownOutcome()
        {
            try
            {
                SpeedControlScreen speedControl = SpeedControlScreen.Instance;
                if (speedControl == null)
                {
                    return " Emergency pause could not be confirmed because SpeedControlScreen is unavailable.";
                }

                if (!speedControl.IsPaused)
                {
                    speedControl.Pause(false, false);
                }

                return speedControl.IsPaused
                    ? " The game is paused and the mutation safety latch is set."
                    : " Emergency pause was requested but could not be confirmed; the mutation safety latch is set.";
            }
            catch (Exception ex)
            {
                return " Emergency pause raised " + ex.GetBaseException().Message +
                    "; the mutation safety latch is set.";
            }
        }

        private static bool VerifyActionSpecific(JObject state, JObject command, string action, out string error)
        {
            if (action == "set_paused")
            {
                return EvaluateCondition(
                    state,
                    new JObject
                    {
                        ["path"] = "game.paused",
                        ["op"] = "eq",
                        ["value"] = command["args"]["paused"],
                        ["quality"] = new JArray("observed"),
                        ["quality_contract_id"] = CommandFieldContracts.GamePausedId
                    },
                    out error);
            }

            if (action == "set_research")
            {
                return EvaluateCondition(
                    state,
                    new JObject
                    {
                        ["path"] = "research.current",
                        ["op"] = "eq",
                        ["value"] = command["args"]["tech"],
                        ["quality"] = new JArray("observed"),
                        ["quality_contract_id"] = CommandFieldContracts.ResearchCurrentId
                    },
                    out error);
            }

            if (action == "set_duplicant_chore_priority")
            {
                int instanceId = command["args"]["duplicant_instance_id"].Value<int>();
                string groupId = command["args"]["chore_group"].ToString();
                return EvaluateCondition(
                    state,
                    new JObject
                    {
                        ["path"] = DuplicantPriorityPath(instanceId, groupId),
                        ["op"] = "eq",
                        ["value"] = command["args"]["priority"],
                        ["quality"] = new JArray("observed"),
                        ["quality_contract_id"] = CommandFieldContracts.DuplicantPriorityId
                    },
                    out error);
            }

            if (action == "mark_dig_cells")
            {
                if (!TryReadUniqueCells(command["args"]["cells"], out List<int> cells, out error))
                {
                    return false;
                }

                foreach (int cell in cells)
                {
                    Diggable designation = Diggable.GetDiggable(cell);
                    KPrefabID prefab = designation?.GetComponent<KPrefabID>();
                    if (designation == null ||
                        prefab == null ||
                        prefab.InstanceID <= 0 ||
                        designation.digTypeFlags != (int)Diggable.DiggableType.Tile ||
                        !Grid.Solid[cell])
                    {
                        error = "Marked dig cell is missing its exact tile-only designation: " +
                            cell.ToString(CultureInfo.InvariantCulture);
                        return false;
                    }
                }

                error = null;
                return true;
            }

            if (action == "cancel_dig_cells")
            {
                if (!TryReadDigCancelTargets(command["args"]["targets"], out List<DigCancelTarget> targets, out error))
                {
                    return false;
                }

                foreach (DigCancelTarget target in targets)
                {
                    if (Diggable.GetDiggable(target.Cell) != null || !Grid.Solid[target.Cell])
                    {
                        error = "Cancelled dig cell still has a designation or no longer contains its solid tile: " +
                            target.Cell.ToString(CultureInfo.InvariantCulture);
                        return false;
                    }
                }

                error = null;
                return true;
            }

            if (action == "place_building_blueprint")
            {
                return VerifyPlacedBlueprints(command["args"] as JObject, isUtilityPath: false, out error);
            }

            if (action == "place_utility_blueprint_path")
            {
                return VerifyPlacedBlueprints(command["args"] as JObject, isUtilityPath: true, out error);
            }

            if (action == "cancel_building_blueprint")
            {
                return VerifyCancelledBlueprints(
                    command["args"] as JObject,
                    expectedUtility: false,
                    out error);
            }

            if (action == "cancel_utility_blueprint_path")
            {
                return VerifyCancelledBlueprints(
                    command["args"] as JObject,
                    expectedUtility: true,
                    out error);
            }

            error = null;
            return true;
        }

        private static bool VerifyPlacedBlueprints(JObject args, bool isUtilityPath, out string error)
        {
            string expectedDefId;
            if (isUtilityPath
                ? !TryReadUtilityBlueprintDefId(args, out expectedDefId, out error)
                : !TryReadBuildingBlueprintDefId(args, out expectedDefId, out error))
            {
                return false;
            }
            int maximum = isUtilityPath ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
            if (!TryReadBlueprintPlacementArgs(
                    args,
                    expectedDefId,
                    maximum,
                    isUtilityPath,
                    out string pathId,
                    out _,
                    out List<int> cells,
                    out List<Tag> materials,
                    out error))
            {
                return false;
            }
            List<int> instanceIds = new List<int>();
            foreach (int cell in cells)
            {
                if (!ProbeStateReader.TryFindBlueprintAtCell(
                        cell,
                        out GameObject blueprint,
                        out Constructable constructable,
                        out Building building,
                        out int instanceId) ||
                    blueprint.GetComponent<BuildingComplete>() != null ||
                    !string.Equals(building.Def?.PrefabID, expectedDefId, StringComparison.Ordinal) ||
                    building.Def.isUtility != isUtilityPath ||
                    building.Orientation != Orientation.Neutral ||
                    !TryReadBlueprintConstructionState(constructable, out bool inProgress, out _) ||
                    inProgress ||
                    !TagsEqual(constructable.SelectedElementsTags, materials))
                {
                    error = "Placed blueprint does not match its exact definition, cell, materials, or unfinished state: " +
                        cell.ToString(CultureInfo.InvariantCulture);
                    return false;
                }
                instanceIds.Add(instanceId);
            }
            if (isUtilityPath && !ProbeStateReader.UtilityPathMatches(pathId, expectedDefId, cells, instanceIds))
            {
                error = "Placed utility blueprints do not match the session path registry.";
                return false;
            }
            error = null;
            return true;
        }

        private static bool VerifyCancelledBlueprints(JObject args, bool expectedUtility, out string error)
        {
            int maximum = expectedUtility ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
            if (!TryReadBlueprintCancelTargets(
                    args?["targets"],
                    maximum,
                    out List<BlueprintCancelTarget> targets,
                    out error))
            {
                return false;
            }
            foreach (BlueprintCancelTarget target in targets)
            {
                if (ProbeStateReader.TryFindBlueprint(
                        target.Cell,
                        target.BlueprintInstanceId,
                        out _,
                        out _,
                        out _))
                {
                    error = "Cancelled blueprint instance is still present: " +
                        target.BlueprintInstanceId.ToString(CultureInfo.InvariantCulture);
                    return false;
                }
            }
            error = null;
            return true;
        }

        private static bool EvaluateConditions(JObject state, JArray conditions, out string error)
        {
            if (conditions == null)
            {
                error = "Condition list is missing.";
                return false;
            }

            foreach (JToken token in conditions)
            {
                if (!EvaluateCondition(state, (JObject)token, out error))
                {
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static bool EvaluateCondition(JObject state, JObject condition, out string error)
        {
            string path = ReadString(condition, "path");
            string op = ReadString(condition, "op");
            JToken actual = state.SelectToken(path, false);
            if (actual == null)
            {
                error = path + " is unavailable.";
                return false;
            }

            if (!CommandFieldContracts.ValidateCurrentState(state, condition, actual, out string contractError))
            {
                error = contractError;
                return false;
            }

            JObject qualityMap = state["quality"] as JObject;
            string status = ReadString(qualityMap?[path] as JObject, "status");
            if (string.IsNullOrEmpty(status) || status == "unsupported" || status == "unknown" || status == "error" || status == "stale")
            {
                error = path + " quality is not trustworthy: " + (status ?? "missing");
                return false;
            }

            if (!QualityAllowed(condition["quality"], status))
            {
                error = path + " quality " + status + " is outside the condition allowance.";
                return false;
            }

            JToken expected = condition["value"];
            bool matches;
            switch (op)
            {
                case "eq":
                    matches = TokenEquals(actual, expected);
                    break;
                case "ne":
                    matches = !TokenEquals(actual, expected);
                    break;
                case "lt":
                case "lte":
                case "gt":
                case "gte":
                    matches = CompareNumbers(actual, expected, op);
                    break;
                case "contains":
                    matches = ContainsValue(actual, expected);
                    break;
                default:
                    matches = false;
                    break;
            }

            error = matches
                ? null
                : path + " did not satisfy " + op + " " + expected.ToString(Formatting.None) + ".";
            return matches;
        }

        private static JObject CaptureCurrentState()
        {
            JObject state = JObject.FromObject(ProbeStateReader.ReadState());
            string capturedAt = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
            state["session_id"] = ProbeSession.CurrentSessionId;
            state["save_id_hash"] = ProbeSession.CurrentSaveIdHash;
            state["world_id"] = ProbeSession.CurrentWorldId;
            state["ts"] = capturedAt;
            state["captured_at"] = capturedAt;
            state["capture_source"] = "executor_direct_read";
            return state;
        }

        private static JObject LoadPolicy(out string error)
        {
            string path = Path.Combine(ProbeWriter.BridgeDirectory, "action-policy.json");
            if (!File.Exists(path))
            {
                error = "Action policy is missing; actions default to disabled.";
                return null;
            }

            try
            {
                JObject policy = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                error = null;
                return policy;
            }
            catch (Exception ex)
            {
                error = "Action policy is invalid: " + ex.GetBaseException().Message;
                return null;
            }
        }

        private static void QuarantineCommand(
            string source,
            CommandDirectories dirs,
            JObject command,
            string ackStatus,
            string reason,
            string classification)
        {
            command["status"] = "quarantined";
            command["quarantined_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
            command["quarantine_reason"] = reason;
            command["ack_status"] = ackStatus;
            if (!string.IsNullOrEmpty(classification))
            {
                command["classification"] = classification;
            }

            MoveCommandWithPayload(source, dirs.Quarantine, command);
        }

        private static void QuarantineFile(
            string source,
            CommandDirectories dirs,
            string ackStatus,
            string reasonCode,
            string reason)
        {
            string id = Path.GetFileNameWithoutExtension(source);
            string action = "unknown";
            try
            {
                JObject command = ParseObject(File.ReadAllText(source, Encoding.UTF8));
                id = ReadString(command, "id") ?? id;
                action = ReadString(command, "action") ?? action;
                Ack(id, action, ackStatus, reason, "behavior_fail");
                command["status"] = "quarantined";
                command["quarantined_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
                command["quarantine_reason"] = reasonCode;
                command["quarantine_detail"] = reason;
                command["ack_status"] = ackStatus;
                command["classification"] = "behavior_fail";
                MoveCommandWithPayload(source, dirs.Quarantine, command);
            }
            catch
            {
                Ack(id, action, ackStatus, reason, "behavior_fail");
                MoveRawFile(source, dirs.Quarantine);
            }
        }

        private static bool FinalizeCommand(
            string source,
            string targetDirectory,
            JObject command,
            string status,
            string classification,
            string message,
            JObject beforeState,
            JObject afterState)
        {
            command["status"] = status;
            command["completed_at"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
            JObject result = new JObject
            {
                ["message"] = message,
                ["before_state"] = beforeState,
                ["after_state"] = afterState
            };
            if (!string.IsNullOrEmpty(classification))
            {
                result["classification"] = classification;
            }

            command["result"] = result;
            return MoveCommandWithPayload(source, targetDirectory, command);
        }

        private static bool MoveCommandWithPayload(string source, string targetDirectory, JObject payload)
        {
            try
            {
                if (string.Equals(
                        Path.GetFileName(Path.GetDirectoryName(source)),
                        "executing",
                        StringComparison.OrdinalIgnoreCase))
                {
                    string attemptId = ReadString(payload, "attempt_id");
                    string commandIntentHash = ReadString(payload, "command_intent_hash");
                    if (ExecutionClaimStore.TryTransitionExecuting(
                            source,
                            targetDirectory,
                            payload,
                            attemptId,
                            commandIntentHash,
                            out _,
                            out string transitionError))
                    {
                        return true;
                    }
                    ProbeWriter.WriteError("execution_claim_transition", transitionError);
                    return false;
                }

                Directory.CreateDirectory(targetDirectory);
                string target = UniqueTarget(targetDirectory, Path.GetFileNameWithoutExtension(source));
                string temp = target + ".tmp-" + Guid.NewGuid().ToString("N");
                File.WriteAllText(temp, payload.ToString(Formatting.Indented) + Environment.NewLine, new UTF8Encoding(false));
                File.Move(temp, target);
                if (File.Exists(source))
                {
                    File.Delete(source);
                }
                return true;
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("command_move", ex);
                return false;
            }
        }

        private static void MoveRawFile(string source, string targetDirectory)
        {
            try
            {
                Directory.CreateDirectory(targetDirectory);
                File.Move(source, UniqueTarget(targetDirectory, Path.GetFileNameWithoutExtension(source)));
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("command_move", ex);
            }
        }

        private static string UniqueTarget(string directory, string stem)
        {
            string target = Path.Combine(directory, stem + ".json");
            if (!File.Exists(target))
            {
                return target;
            }

            return Path.Combine(
                directory,
                stem + "-" + DateTimeOffset.Now.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture) + ".json");
        }

        private static CommandDirectories EnsureCommandDirectories()
        {
            string root = Path.Combine(ProbeWriter.BridgeDirectory, "commands");
            CommandDirectories dirs = new CommandDirectories(root);
            foreach (string directory in dirs.All)
            {
                Directory.CreateDirectory(directory);
            }

            return dirs;
        }

        private static bool HasProcessedId(CommandDirectories dirs, string id)
        {
            if (InFlightIds.Contains(id))
            {
                return true;
            }

            foreach (string directory in new[]
            {
                dirs.Executing,
                dirs.Approving,
                dirs.Confirming,
                dirs.Done,
                dirs.Failed,
                dirs.Rejected,
                dirs.Quarantine
            })
            {
                foreach (string path in Directory.GetFiles(directory, "*.json"))
                {
                    if (string.Equals(Path.GetFileNameWithoutExtension(path), id, StringComparison.Ordinal))
                    {
                        return true;
                    }

                    try
                    {
                        JObject value = ParseObject(File.ReadAllText(path, Encoding.UTF8));
                        if (string.Equals(ReadString(value, "id"), id, StringComparison.Ordinal))
                        {
                            return true;
                        }
                    }
                    catch
                    {
                        // Unreadable historical files do not authorize replay.
                    }
                }
            }

            return false;
        }

        private static long Ack(
            string id,
            string action,
            string status,
            string message,
            string classification,
            string attemptId = null,
            string commandIntentHash = null)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>
            {
                { "id", id },
                { "action", action },
                { "status", status },
                { "stage", status },
                { "message", message }
            };
            if (!string.IsNullOrEmpty(attemptId))
            {
                payload["attempt_id"] = attemptId;
            }
            if (!string.IsNullOrEmpty(commandIntentHash))
            {
                payload["command_intent_hash"] = commandIntentHash;
            }
            if (!string.IsNullOrEmpty(classification))
            {
                payload["classification"] = classification;
            }

            return ProbeWriter.WriteEvent("command_ack", payload);
        }

        private static bool PolicyContains(JObject policy, string key, string value)
        {
            JArray values = policy[key] as JArray;
            if (values == null || string.IsNullOrEmpty(value))
            {
                return false;
            }

            foreach (JToken token in values)
            {
                if (token.Type == JTokenType.String && string.Equals(token.ToString(), value, StringComparison.Ordinal))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool ContainsExpectedEquality(JArray conditions, string path, JToken expected)
        {
            foreach (JObject condition in conditions.Children<JObject>())
            {
                if (string.Equals(ReadString(condition, "path"), path, StringComparison.Ordinal) &&
                    string.Equals(ReadString(condition, "op"), "eq", StringComparison.Ordinal) &&
                    TokenEquals(condition["value"], expected))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool ContainsObservedEquality(JArray conditions, string path, JToken expected)
        {
            foreach (JObject condition in conditions.Children<JObject>())
            {
                if (string.Equals(ReadString(condition, "path"), path, StringComparison.Ordinal) &&
                    string.Equals(ReadString(condition, "op"), "eq", StringComparison.Ordinal) &&
                    TokenEquals(condition["value"], expected) &&
                    IsObservedOnly(condition["quality"]) &&
                    CommandFieldContracts.DeclarationMatches(condition, out _))
                {
                    return true;
                }
            }

            return false;
        }

        private static readonly string[] OnsetAlertIds = new[]
        {
            CanonicalAlertCatalog.Exhaustion,
            CanonicalAlertCatalog.Hyperthermia,
            CanonicalAlertCatalog.Hypothermia,
            CanonicalAlertCatalog.LowOxygen,
            CanonicalAlertCatalog.UrgentToilet,
        };

        /// <summary>
        /// True when the alerts precondition is observed and holds only onset
        /// alerts, i.e. needs that clear by themselves once the game runs.
        /// </summary>
        private static bool ContainsOnlyOnsetAlerts(JArray conditions)
        {
            foreach (JObject condition in conditions.Children<JObject>())
            {
                if (!string.Equals(ReadString(condition, "path"), "alerts", StringComparison.Ordinal) ||
                    !string.Equals(ReadString(condition, "op"), "eq", StringComparison.Ordinal) ||
                    !IsObservedOnly(condition["quality"]) ||
                    !CommandFieldContracts.DeclarationMatches(condition, out _))
                {
                    continue;
                }

                if (condition["value"]?.Type != JTokenType.Array)
                {
                    continue;
                }

                bool onlyOnset = true;
                foreach (JToken item in condition["value"].Children())
                {
                    if (item.Type != JTokenType.String ||
                        Array.IndexOf(OnsetAlertIds, item.ToString()) < 0)
                    {
                        onlyOnset = false;
                        break;
                    }
                }

                if (onlyOnset)
                {
                    return true;
                }
            }

            return false;
        }

        private static string ValidateDigConditionContract(
            JObject command,
            string action,
            JArray preconditions,
            JArray expectedEffects)
        {
            if (!ContainsObservedEquality(preconditions, "game.paused", new JValue(true)))
            {
                return action + " requires observed game.paused=true.";
            }

            List<int> cells;
            Dictionary<int, int> targetInstances = new Dictionary<int, int>();
            if (action == "mark_dig_cells")
            {
                if (!TryReadUniqueCells(command["args"]["cells"], out cells, out string cellsError))
                {
                    return cellsError;
                }

                if (!ContainsObservedEquality(
                        preconditions,
                        "designations.dig.tool.tiles_filter_on",
                        new JValue(true)) ||
                    !ContainsObservedEquality(
                        preconditions,
                        "designations.dig.tool.natural_backwall_filter_on",
                        new JValue(false)))
                {
                    return "mark_dig_cells must bind observed Tiles=true and NaturalBackwall=false.";
                }
            }
            else
            {
                if (!TryReadDigCancelTargets(
                        command["args"]["targets"],
                        out List<DigCancelTarget> targets,
                        out string targetsError))
                {
                    return targetsError;
                }

                cells = targets.Select(target => target.Cell).ToList();
                foreach (DigCancelTarget target in targets)
                {
                    targetInstances[target.Cell] = target.DesignationInstanceId;
                }
            }

            foreach (int cell in cells)
            {
                string prefix = DigCellPath(cell);
                if (!TryGetObservedEquality(preconditions, prefix + ".fingerprint", out JToken fingerprint) ||
                    fingerprint.Type != JTokenType.String ||
                    !fingerprint.ToString().StartsWith("sha256:", StringComparison.Ordinal))
                {
                    return action + " must bind an observed state fingerprint for every cell.";
                }

                if (!HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".element_id") ||
                    !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".mass_kg") ||
                    !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".damage"))
                {
                    return action + " must prove element, mass, and damage remain unchanged.";
                }

                if (!ContainsObservedEquality(preconditions, prefix + ".solid", new JValue(true)) ||
                    !ContainsObservedEquality(expectedEffects, prefix + ".solid", new JValue(true)))
                {
                    return action + " must bind and prove that the target tile remains solid.";
                }

                if (action == "mark_dig_cells")
                {
                    if (!ContainsObservedEquality(preconditions, prefix + ".safe_mark_candidate", new JValue(true)) ||
                        !ContainsObservedEquality(preconditions, prefix + ".designation_present", new JValue(false)) ||
                        !ContainsObservedEquality(expectedEffects, prefix + ".designation_present", new JValue(true)) ||
                        !ContainsObservedCondition(expectedEffects, prefix + ".designation_instance_id", "gt", new JValue(0)) ||
                        !ContainsObservedEquality(expectedEffects, prefix + ".designation_kind", new JValue("tile")) ||
                        !ContainsObservedEquality(expectedEffects, prefix + ".safe_mark_candidate", new JValue(false)))
                    {
                        return "mark_dig_cells is missing its exact designation create contract.";
                    }
                }
                else if (!ContainsObservedEquality(
                             preconditions,
                             prefix + ".designation_instance_id",
                             new JValue(targetInstances[cell])) ||
                    !ContainsObservedEquality(preconditions, prefix + ".designation_kind", new JValue("tile")) ||
                    !ContainsObservedEquality(preconditions, prefix + ".designation_in_progress", new JValue(false)) ||
                    !ContainsObservedEquality(expectedEffects, prefix + ".designation_present", new JValue(false)) ||
                    !ContainsObservedEquality(expectedEffects, prefix + ".designation_instance_id", new JValue(0)) ||
                    !ContainsObservedEquality(expectedEffects, prefix + ".designation_kind", new JValue("none")) ||
                    !ContainsObservedEquality(expectedEffects, prefix + ".safe_mark_candidate", new JValue(true)))
                {
                    return "cancel_dig_cells is missing its exact designation removal contract.";
                }
            }

            return null;
        }

        private static string ValidateBlueprintConditionContract(
            JObject command,
            string action,
            JArray preconditions,
            JArray expectedEffects)
        {
            if (!ContainsObservedEquality(preconditions, "game.paused", new JValue(true)) ||
                !ContainsObservedEquality(preconditions, "game.sandbox_mode", new JValue(false)) ||
                !ContainsObservedEquality(preconditions, "game.instant_build_mode", new JValue(false)))
            {
                return action + " requires observed paused=true, sandbox=false, and instant_build=false.";
            }

            bool placement = action == "place_building_blueprint" ||
                action == "place_utility_blueprint_path";
            bool utility = action == "place_utility_blueprint_path" ||
                action == "cancel_utility_blueprint_path";
            string defId;
            if (utility && placement)
            {
                if (!TryReadUtilityBlueprintDefId(command["args"] as JObject, out defId, out string utilityError))
                {
                    return action + ": " + utilityError;
                }
            }
            else if (utility)
            {
                // A cancellation names its utility only through the path it
                // binds; the exact path contract below then checks every field.
                if (!TryReadPathId(command["args"] as JObject, "path_id", out string cancelPathId) ||
                    !TryGetObservedEquality(preconditions, UtilityPathPath(cancelPathId) + ".building_def_id", out JToken boundDefId) ||
                    boundDefId.Type != JTokenType.String ||
                    !ProbeStateReader.IsUtilityBlueprintDef(boundDefId.ToString()))
                {
                    return action + " must bind the allowlisted utility of its path.";
                }
                defId = boundDefId.ToString();
            }
            else if (placement)
            {
                if (!TryReadBuildingBlueprintDefId(command["args"] as JObject, out defId, out string defError))
                {
                    return action + ": " + defError;
                }
            }
            else if (!TryReadCancelledBuildingDefId(command, preconditions, out defId, out string cancelDefError))
            {
                return action + ": " + cancelDefError;
            }
            string canPlaceField = ProbeStateReader.BlueprintCanPlaceField(defId);
            if (placement)
            {
                int maximum = utility ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
                if (!TryReadBlueprintPlacementArgs(
                        command["args"] as JObject,
                        defId,
                        maximum,
                        utility,
                        out string pathId,
                        out _,
                        out List<int> cells,
                        out List<Tag> materialTags,
                        out string parseError))
                {
                    return parseError;
                }
                JArray materialValues = new JArray(materialTags.Select(tag => tag.Name));
                string catalogPrefix = "blueprints.catalog." + defId;
                if (!ContainsObservedEquality(preconditions, catalogPrefix + ".available", new JValue(true)) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".prefab_id", new JValue(defId)) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".is_utility", new JValue(utility)) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".cancellable", new JValue(true)) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".orientation", new JValue("Neutral")) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".facade_id", new JValue("DEFAULT_FACADE")) ||
                    !ContainsObservedEquality(preconditions, catalogPrefix + ".material_tags", materialValues))
                {
                    return action + " must bind the exact observed blueprint catalog and material selection.";
                }
                foreach (int cell in cells)
                {
                    string cellError = ValidateBlueprintPlaceCellContract(
                        cell,
                        defId,
                        utility,
                        canPlaceField,
                        materialValues,
                        preconditions,
                        expectedEffects);
                    if (cellError != null)
                    {
                        return cellError;
                    }
                }
                if (utility)
                {
                    string pathPrefix = UtilityPathPath(pathId);
                    JArray cellValues = new JArray(cells);
                    if (!ContainsObservedEquality(expectedEffects, pathPrefix + ".path_id", new JValue(pathId)) ||
                        !ContainsObservedEquality(expectedEffects, pathPrefix + ".building_def_id", new JValue(defId)) ||
                        !ContainsObservedEquality(expectedEffects, pathPrefix + ".cells", cellValues) ||
                        !ContainsObservedEquality(expectedEffects, pathPrefix + ".active", new JValue(true)) ||
                        !ContainsObservedEquality(expectedEffects, pathPrefix + ".fully_active", new JValue(true)))
                    {
                        return "place_utility_blueprint_path must bind the exact active path postcondition.";
                    }
                }
                return null;
            }

            int cancelMaximum = utility ? MaxUtilityBlueprintCells : MaxBuildingBlueprintCells;
            if (!TryReadBlueprintCancelTargets(
                    command["args"]?["targets"],
                    cancelMaximum,
                    out List<BlueprintCancelTarget> targets,
                    out string targetsError))
            {
                return targetsError;
            }
            foreach (BlueprintCancelTarget target in targets)
            {
                string cellError = ValidateBlueprintCancelCellContract(
                    target,
                    defId,
                    utility,
                    canPlaceField,
                    preconditions,
                    expectedEffects);
                if (cellError != null)
                {
                    return cellError;
                }
            }
            if (utility)
            {
                if (!TryReadPathId(command["args"] as JObject, "path_id", out string pathId))
                {
                    return "cancel_utility_blueprint_path has an invalid path_id.";
                }
                string pathPrefix = UtilityPathPath(pathId);
                JArray cells = new JArray(targets.Select(target => target.Cell));
                JArray instanceIds = new JArray(targets.Select(target => target.BlueprintInstanceId));
                if (!ContainsObservedEquality(preconditions, pathPrefix + ".path_id", new JValue(pathId)) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".building_def_id", new JValue(defId)) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".cells", cells) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".blueprint_instance_ids", instanceIds) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".active_blueprint_instance_ids", instanceIds) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".active", new JValue(true)) ||
                    !ContainsObservedEquality(preconditions, pathPrefix + ".fully_active", new JValue(true)) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".path_id", new JValue(pathId)) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".building_def_id", new JValue(defId)) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".cells", cells) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".blueprint_instance_ids", instanceIds) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".active_blueprint_instance_ids", new JArray()) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".active", new JValue(false)) ||
                    !ContainsObservedEquality(expectedEffects, pathPrefix + ".fully_active", new JValue(false)))
                {
                    return "cancel_utility_blueprint_path must bind the exact path removal contract.";
                }
            }
            return null;
        }

        private static string ValidateBlueprintPlaceCellContract(
            int cell,
            string defId,
            bool utility,
            string canPlaceField,
            JArray materialTags,
            JArray preconditions,
            JArray expectedEffects)
        {
            string prefix = BlueprintCellPath(cell);
            if (!TryGetObservedEquality(preconditions, prefix + ".fingerprint", out JToken fingerprint) ||
                fingerprint.Type != JTokenType.String ||
                !fingerprint.ToString().StartsWith("sha256:", StringComparison.Ordinal) ||
                !TryGetObservedEquality(preconditions, prefix + ".empty_fingerprint", out JToken emptyFingerprint) ||
                emptyFingerprint.Type != JTokenType.String ||
                !emptyFingerprint.ToString().StartsWith("sha256:", StringComparison.Ordinal) ||
                !TryGetObservedEquality(preconditions, prefix + ".object_count", out JToken objectCount) ||
                objectCount.Type != JTokenType.Integer ||
                objectCount.Value<long>() < 0)
            {
                return "Blueprint placement must bind observed current and empty fingerprints and object count for every cell.";
            }
            // Only a utility may share its cell with what is already there: a
            // wire or pipe connects a port by occupying the port's cell (roadmap
            // B15).  An empty cell keeps the exact empty-cell contract.
            long existing = objectCount.Value<long>();
            if ((existing != 0 && !utility) ||
                (existing == 0 && !TokenEquals(fingerprint, emptyFingerprint)))
            {
                return "Blueprint placement must bind matching observed empty and current fingerprints for every cell.";
            }
            if (!HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".element_id") ||
                !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".mass_kg") ||
                !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".damage") ||
                !ContainsObservedEquality(expectedEffects, prefix + ".empty_fingerprint", emptyFingerprint))
            {
                return "Blueprint placement must prove physical cell state and empty fingerprint remain unchanged.";
            }
            bool emptyBound = utility
                ? TryGetObservedEquality(preconditions, prefix + ".empty_for_blueprint", out JToken emptyFlag) &&
                    emptyFlag.Type == JTokenType.Boolean
                : ContainsObservedEquality(preconditions, prefix + ".empty_for_blueprint", new JValue(true));
            if (!emptyBound ||
                !ContainsObservedEquality(preconditions, prefix + "." + canPlaceField, new JValue(true)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_present", new JValue(false)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_instance_id", new JValue(0)) ||
                !ContainsObservedCondition(expectedEffects, prefix + ".fingerprint", "ne", fingerprint) ||
                !ContainsObservedCondition(expectedEffects, prefix + ".object_count", "gt", new JValue(existing)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".empty_for_blueprint", new JValue(false)) ||
                !ContainsObservedEquality(expectedEffects, prefix + "." + canPlaceField, new JValue(false)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_present", new JValue(true)) ||
                !ContainsObservedCondition(expectedEffects, prefix + ".blueprint_instance_id", "gt", new JValue(0)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_prefab_id", new JValue(defId)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_orientation", new JValue("Neutral")) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_material_tags", materialTags) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_in_progress", new JValue(false)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_is_utility", new JValue(utility)))
            {
                return "Blueprint placement is missing its exact create contract for cell " +
                    cell.ToString(CultureInfo.InvariantCulture) + ".";
            }
            return null;
        }

        private static string ValidateBlueprintCancelCellContract(
            BlueprintCancelTarget target,
            string defId,
            bool utility,
            string canPlaceField,
            JArray preconditions,
            JArray expectedEffects)
        {
            string prefix = BlueprintCellPath(target.Cell);
            string instancePrefix = BlueprintInstancePath(target.BlueprintInstanceId);
            if (!TryGetObservedEquality(preconditions, prefix + ".fingerprint", out JToken fingerprint) ||
                fingerprint.Type != JTokenType.String ||
                !fingerprint.ToString().StartsWith("sha256:", StringComparison.Ordinal) ||
                !TryGetObservedEquality(preconditions, prefix + ".empty_fingerprint", out JToken emptyFingerprint) ||
                emptyFingerprint.Type != JTokenType.String ||
                !emptyFingerprint.ToString().StartsWith("sha256:", StringComparison.Ordinal))
            {
                return "Blueprint cancellation must bind current and restoration fingerprints for every cell.";
            }
            if (!HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".element_id") ||
                !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".mass_kg") ||
                !HasObservedUnchangedValue(preconditions, expectedEffects, prefix + ".damage"))
            {
                return "Blueprint cancellation must prove physical cell state remains unchanged.";
            }
            if (!ContainsObservedEquality(preconditions, prefix + ".blueprint_present", new JValue(true)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_instance_id", new JValue(target.BlueprintInstanceId)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_prefab_id", new JValue(defId)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_orientation", new JValue("Neutral")) ||
                !TryGetObservedEquality(preconditions, prefix + ".blueprint_material_tags", out JToken materials) ||
                !(materials is JArray materialArray) ||
                materialArray.Count == 0 ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_in_progress", new JValue(false)) ||
                !ContainsObservedEquality(preconditions, prefix + ".blueprint_is_utility", new JValue(utility)) ||
                !ContainsObservedEquality(preconditions, instancePrefix + ".instance_id", new JValue(target.BlueprintInstanceId)) ||
                !ContainsObservedEquality(preconditions, instancePrefix + ".cell", new JValue(target.Cell)) ||
                !ContainsObservedEquality(preconditions, instancePrefix + ".prefab_id", new JValue(defId)) ||
                !ContainsObservedEquality(preconditions, instancePrefix + ".in_progress", new JValue(false)))
            {
                return "Blueprint cancellation is missing its exact instance precondition contract.";
            }
            // A utility may have been laid over a building's port cell; its
            // cancellation then restores the cell minus exactly the blueprint,
            // not an empty cell.  Everything else restores an empty cell.
            long remaining = 0;
            if (utility)
            {
                if (!TryGetObservedEquality(preconditions, prefix + ".object_count", out JToken objectCount) ||
                    objectCount.Type != JTokenType.Integer ||
                    objectCount.Value<long>() < 1)
                {
                    return "Utility cancellation must bind the observed object count of every cell.";
                }
                remaining = objectCount.Value<long>() - 1;
            }
            bool restoredFingerprint = remaining == 0
                ? ContainsObservedEquality(expectedEffects, prefix + ".fingerprint", emptyFingerprint)
                : ContainsObservedCondition(expectedEffects, prefix + ".fingerprint", "ne", fingerprint);
            bool restoredEmptiness = utility ||
                ContainsObservedEquality(expectedEffects, prefix + ".empty_for_blueprint", new JValue(true));
            if (!restoredFingerprint ||
                !restoredEmptiness ||
                !ContainsObservedEquality(expectedEffects, prefix + ".empty_fingerprint", emptyFingerprint) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".object_count", new JValue(remaining)) ||
                !ContainsObservedEquality(expectedEffects, prefix + "." + canPlaceField, new JValue(true)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_present", new JValue(false)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_instance_id", new JValue(0)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_prefab_id", new JValue("none")) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_orientation", new JValue("none")) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_material_tags", new JArray()) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_in_progress", new JValue(false)) ||
                !ContainsObservedEquality(expectedEffects, prefix + ".blueprint_is_utility", new JValue(false)))
            {
                return "Blueprint cancellation is missing its exact restoration contract.";
            }
            return null;
        }

        private static bool HasObservedUnchangedValue(JArray before, JArray after, string path)
        {
            return TryGetObservedEquality(before, path, out JToken expected) &&
                ContainsObservedEquality(after, path, expected);
        }

        private static bool TryGetObservedEquality(JArray conditions, string path, out JToken value)
        {
            foreach (JObject condition in conditions.Children<JObject>())
            {
                if (string.Equals(ReadString(condition, "path"), path, StringComparison.Ordinal) &&
                    string.Equals(ReadString(condition, "op"), "eq", StringComparison.Ordinal) &&
                    condition["value"] != null &&
                    IsObservedOnly(condition["quality"]) &&
                    CommandFieldContracts.DeclarationMatches(condition, out _))
                {
                    value = condition["value"];
                    return true;
                }
            }

            value = null;
            return false;
        }

        private static bool ContainsObservedCondition(
            JArray conditions,
            string path,
            string op,
            JToken expected)
        {
            foreach (JObject condition in conditions.Children<JObject>())
            {
                if (string.Equals(ReadString(condition, "path"), path, StringComparison.Ordinal) &&
                    string.Equals(ReadString(condition, "op"), op, StringComparison.Ordinal) &&
                    TokenEquals(condition["value"], expected) &&
                    IsObservedOnly(condition["quality"]) &&
                    CommandFieldContracts.DeclarationMatches(condition, out _))
                {
                    return true;
                }
            }

            return false;
        }

        private static string DigCellPath(int cell)
        {
            return "designations.dig.by_cell." + ProbeStateReader.DigCellKey(cell);
        }

        private static string BlueprintCellPath(int cell)
        {
            return "cells.by_id." + ProbeStateReader.DigCellKey(cell);
        }

        private static string BlueprintInstancePath(int instanceId)
        {
            return "blueprints.by_instance_id.id_" + instanceId.ToString(CultureInfo.InvariantCulture);
        }

        private static string UtilityPathPath(string pathId)
        {
            return "blueprints.utility_paths." + ProbeStateReader.UtilityPathKey(pathId);
        }

        private static bool IsObservedOnly(JToken requirement)
        {
            if (requirement?.Type == JTokenType.String)
            {
                return string.Equals(requirement.ToString(), "observed", StringComparison.Ordinal);
            }

            if (requirement is JArray array && array.Count == 1)
            {
                return array[0].Type == JTokenType.String &&
                    string.Equals(array[0].ToString(), "observed", StringComparison.Ordinal);
            }

            return false;
        }

        private static string DuplicantPriorityPath(int instanceId, string groupId)
        {
            return "duplicants.by_instance_id." + ProbeStateReader.DuplicantStateKey(instanceId) +
                ".chore_priorities." + groupId;
        }

        private static bool QualityAllowed(JToken requirement, string actual)
        {
            if (requirement == null)
            {
                return actual == "observed" || actual == "estimated";
            }

            if (requirement.Type == JTokenType.String)
            {
                return string.Equals(requirement.ToString(), actual, StringComparison.Ordinal);
            }

            foreach (JToken token in (JArray)requirement)
            {
                if (token.Type == JTokenType.String && string.Equals(token.ToString(), actual, StringComparison.Ordinal))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool IsStateChangingAction(string action)
        {
            return !string.Equals(action, "noop", StringComparison.Ordinal) &&
                !string.Equals(action, "focus_note", StringComparison.Ordinal);
        }

        private static bool TokenEquals(JToken left, JToken right)
        {
            if (IsNumber(left) && IsNumber(right))
            {
                double actual = left.Value<double>();
                double expected = right.Value<double>();
                if (left.Type == JTokenType.Integer || right.Type == JTokenType.Integer)
                {
                    return actual == expected;
                }

                // JSON.NET emits single-precision game values with fewer digits than
                // an in-memory direct read. Scale the tolerance to that serialization
                // precision while keeping integer identifiers and counters exact.
                double scale = Math.Max(1d, Math.Max(Math.Abs(actual), Math.Abs(expected)));
                return Math.Abs(actual - expected) <= FloatEqualityRelativeTolerance * scale;
            }

            return JToken.DeepEquals(left, right);
        }

        private static bool CompareNumbers(JToken left, JToken right, string op)
        {
            if (!IsNumber(left) || !IsNumber(right))
            {
                return false;
            }

            double actual = left.Value<double>();
            double expected = right.Value<double>();
            switch (op)
            {
                case "lt": return actual < expected;
                case "lte": return actual <= expected;
                case "gt": return actual > expected;
                case "gte": return actual >= expected;
                default: return false;
            }
        }

        private static bool ContainsValue(JToken container, JToken expected)
        {
            if (container.Type == JTokenType.String)
            {
                return container.ToString().IndexOf(expected.ToString(), StringComparison.OrdinalIgnoreCase) >= 0;
            }

            if (container is JArray array)
            {
                foreach (JToken item in array)
                {
                    if (TokenEquals(item, expected))
                    {
                        return true;
                    }
                }
            }

            return false;
        }

        private static bool IsNumber(JToken token)
        {
            return token != null && (token.Type == JTokenType.Integer || token.Type == JTokenType.Float);
        }

        private static bool HasOnlyKeys(JObject obj, params string[] allowed)
        {
            HashSet<string> names = new HashSet<string>(allowed, StringComparer.Ordinal);
            foreach (JProperty property in obj.Properties())
            {
                if (!names.Contains(property.Name))
                {
                    return false;
                }
            }

            return true;
        }

        private static bool IsSafeCommandId(string id)
        {
            if (string.IsNullOrEmpty(id) || id.Length > MaxCommandIdLength || !id.StartsWith("cmd-", StringComparison.Ordinal))
            {
                return false;
            }

            foreach (char ch in id)
            {
                if (!char.IsLetterOrDigit(ch) && ch != '-' && ch != '_' && ch != '.')
                {
                    return false;
                }
            }

            return true;
        }

        private static bool TryReadTimestamp(JObject obj, string key, out DateTimeOffset value)
        {
            string text = ReadString(obj, key);
            return DateTimeOffset.TryParse(
                text,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
                out value);
        }

        private static JObject ParseObject(string json)
        {
            return CanonicalHashContract.ParseObjectStrict(json);
        }

        private static int ReadPositiveInt(JObject obj, string key, int fallback)
        {
            JToken token = obj[key];
            if (token == null || token.Type != JTokenType.Integer)
            {
                return fallback;
            }

            int value = token.Value<int>();
            return value > 0 ? value : fallback;
        }

        private static bool TryReadInt(JObject obj, string key, out int value)
        {
            value = 0;
            JToken token = obj?[key];
            return token?.Type == JTokenType.Integer &&
                int.TryParse(token.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value);
        }

        private static bool TryReadUniqueCells(JToken token, out List<int> cells, out string error)
        {
            return TryReadUniqueCells(token, MaxDigCells, out cells, out error);
        }

        private static bool TryReadUniqueCells(
            JToken token,
            int maximum,
            out List<int> cells,
            out string error)
        {
            cells = new List<int>();
            if (!(token is JArray array) || array.Count < 1 || array.Count > maximum)
            {
                error = "cell array size is outside 1.." + maximum.ToString(CultureInfo.InvariantCulture) + ".";
                return false;
            }

            HashSet<int> seen = new HashSet<int>();
            foreach (JToken item in array)
            {
                if (item.Type != JTokenType.Integer ||
                    !int.TryParse(item.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int cell) ||
                    cell < 0 ||
                    !seen.Add(cell))
                {
                    error = "cell ids must be unique non-negative integers.";
                    return false;
                }

                cells.Add(cell);
            }

            error = null;
            return true;
        }

        // Non-utility blueprint ids the executor accepts; mirrors
        // ProbeStateReader.BlueprintAllowlist without the Wire utility path.
        private static readonly string[] BuildingBlueprintAllowlist =
        {
            "Ladder",
            "MineralDeoxidizer",
            "AlgaeHabitat",
            // The only water-fed oxygen producer.  The world holds no algae but
            // about 5.5 t of water, so this is the colony's remaining oxygen
            // route (2026-10-26 round 38).
            "Electrolyzer",
            // Food production, which the catalog lacked entirely while the
            // colony's stored food reached zero (2026-10-26 round 42).
            "FarmTile",
            "MicrobeMusher",
            // Roadmap B15: the Electrolyzer's power and water supply.
            "LiquidPump",
            "ManualGenerator"
        };

        private static bool IsAllowlistedBuildingBlueprint(string defId)
        {
            return !string.IsNullOrEmpty(defId) && Array.IndexOf(BuildingBlueprintAllowlist, defId) >= 0;
        }

        // Utility paths name their utility (Wire or LiquidConduit) in
        // args.building_def_id; ProbeStateReader.UtilityBlueprintAllowlist is
        // the single list both sides share.
        private static bool TryReadUtilityBlueprintDefId(JObject args, out string defId, out string error)
        {
            defId = args?["building_def_id"]?.Type == JTokenType.String
                ? args["building_def_id"].ToString()
                : null;
            if (!ProbeStateReader.IsUtilityBlueprintDef(defId))
            {
                defId = null;
                error = "building_def_id must name an allowlisted utility blueprint.";
                return false;
            }
            error = null;
            return true;
        }

        private static bool TryReadBuildingBlueprintDefId(JObject args, out string defId, out string error)
        {
            defId = args?["building_def_id"]?.Type == JTokenType.String
                ? args["building_def_id"].ToString()
                : null;
            if (!IsAllowlistedBuildingBlueprint(defId))
            {
                defId = null;
                error = "building_def_id must name an allowlisted single-building blueprint.";
                return false;
            }
            error = null;
            return true;
        }

        // A single-building cancellation names its building through the
        // observed blueprint_prefab_id precondition of every target cell.
        private static bool TryReadCancelledBuildingDefId(
            JObject command,
            JArray preconditions,
            out string defId,
            out string error)
        {
            defId = null;
            if (!TryReadBlueprintCancelTargets(
                    command["args"]?["targets"],
                    MaxBuildingBlueprintCells,
                    out List<BlueprintCancelTarget> targets,
                    out error))
            {
                return false;
            }
            foreach (BlueprintCancelTarget target in targets)
            {
                string prefabId = TryGetObservedEquality(
                        preconditions,
                        BlueprintCellPath(target.Cell) + ".blueprint_prefab_id",
                        out JToken prefab) && prefab.Type == JTokenType.String
                    ? prefab.ToString()
                    : null;
                if (!IsAllowlistedBuildingBlueprint(prefabId) ||
                    (defId != null && !string.Equals(defId, prefabId, StringComparison.Ordinal)))
                {
                    defId = null;
                    error = "cancel_building_blueprint must bind one allowlisted observed blueprint_prefab_id for every target.";
                    return false;
                }
                defId = prefabId;
            }
            if (defId == null)
            {
                error = "cancel_building_blueprint requires at least one target.";
                return false;
            }
            error = null;
            return true;
        }

        private static bool TryReadBlueprintPlacementArgs(
            JObject args,
            string expectedDefId,
            int maximum,
            bool requiresPathId,
            out string pathId,
            out BuildingDef def,
            out List<int> cells,
            out List<Tag> materials,
            out string error)
        {
            pathId = null;
            def = null;
            cells = new List<int>();
            materials = new List<Tag>();
            error = null;
            bool shapeValid = args != null && (requiresPathId
                ? HasOnlyKeys(
                    args,
                    "path_id",
                    "building_def_id",
                    "cells",
                    "orientation",
                    "material_tags",
                    "facade_id")
                : HasOnlyKeys(
                    args,
                    "building_def_id",
                    "cells",
                    "orientation",
                    "material_tags",
                    "facade_id"));
            if (!shapeValid ||
                !string.Equals(ReadString(args, "building_def_id"), expectedDefId, StringComparison.Ordinal) ||
                !string.Equals(ReadString(args, "orientation"), "Neutral", StringComparison.Ordinal) ||
                !string.Equals(ReadString(args, "facade_id"), "DEFAULT_FACADE", StringComparison.Ordinal) ||
                !TryReadUniqueCells(args["cells"], maximum, out cells, out error) ||
                !TryReadMaterialTags(args["material_tags"], out materials, out error))
            {
                error = error ?? "argument shape, allowlist id, orientation, facade, cells, or materials are invalid.";
                return false;
            }
            if (requiresPathId)
            {
                if (!TryReadPathId(args, "path_id", out pathId))
                {
                    error = "path_id must be a bounded non-empty alphanumeric id.";
                    return false;
                }
                for (int index = 1; index < cells.Count; index++)
                {
                    if (!Grid.IsValidCell(cells[index - 1]) || !Grid.IsValidCell(cells[index]))
                    {
                        error = "utility path contains a cell outside the game grid.";
                        return false;
                    }
                    Vector2I left = Grid.CellToXY(cells[index - 1]);
                    Vector2I right = Grid.CellToXY(cells[index]);
                    if (Math.Abs(left.x - right.x) + Math.Abs(left.y - right.y) != 1)
                    {
                        error = "utility path cells must be ordered four-neighbor contiguous cells.";
                        return false;
                    }
                }
            }
            def = Assets.GetBuildingDef(expectedDefId);
            if (def == null || def.isUtility != requiresPathId || !def.Cancellable)
            {
                error = "allowlisted BuildingDef is unavailable or does not match its utility/cancellable contract.";
                return false;
            }
            error = null;
            return true;
        }

        private static bool TryReadMaterialTags(JToken token, out List<Tag> materials, out string error)
        {
            materials = new List<Tag>();
            if (!(token is JArray array) || array.Count < 1 || array.Count > MaxMaterialTags)
            {
                error = "material_tags array size is outside 1..8.";
                return false;
            }
            foreach (JToken item in array)
            {
                if (item.Type != JTokenType.String)
                {
                    error = "material tag ids must be strings.";
                    return false;
                }
                string name = item.ToString();
                if (string.IsNullOrEmpty(name) ||
                    name.Length > 64 ||
                    !name.All(character => char.IsLetterOrDigit(character) || character == '_'))
                {
                    error = "material tag ids must be bounded alphanumeric or underscore values.";
                    return false;
                }
                Tag tag = new Tag(name);
                if (!tag.IsValid)
                {
                    error = "material tag id is invalid: " + name;
                    return false;
                }
                materials.Add(tag);
            }
            error = null;
            return true;
        }

        private static bool TryReadPathId(JObject args, string key, out string pathId)
        {
            pathId = ReadString(args, key);
            return !string.IsNullOrEmpty(pathId) &&
                pathId.Length <= MaxBlueprintPathIdLength &&
                pathId.All(char.IsLetterOrDigit);
        }

        private static bool TryReadBlueprintCancelTargets(
            JToken token,
            int maximum,
            out List<BlueprintCancelTarget> targets,
            out string error)
        {
            targets = new List<BlueprintCancelTarget>();
            if (!(token is JArray array) || array.Count < 1 || array.Count > maximum)
            {
                error = "target array size is outside 1.." + maximum.ToString(CultureInfo.InvariantCulture) + ".";
                return false;
            }
            HashSet<int> cells = new HashSet<int>();
            HashSet<int> instances = new HashSet<int>();
            foreach (JToken item in array)
            {
                JObject target = item as JObject;
                if (target == null ||
                    !HasOnlyKeys(target, "cell", "blueprint_instance_id") ||
                    !TryReadInt(target, "cell", out int cell) ||
                    !TryReadInt(target, "blueprint_instance_id", out int instanceId) ||
                    cell < 0 ||
                    instanceId <= 0 ||
                    !cells.Add(cell) ||
                    !instances.Add(instanceId))
                {
                    error = "targets require unique non-negative cells and unique positive blueprint instance ids.";
                    return false;
                }
                targets.Add(new BlueprintCancelTarget
                {
                    Cell = cell,
                    BlueprintInstanceId = instanceId
                });
            }
            error = null;
            return true;
        }

        private static bool TagsEqual(IList<Tag> actual, IList<Tag> expected)
        {
            if (actual == null || expected == null || actual.Count != expected.Count)
            {
                return false;
            }
            for (int index = 0; index < actual.Count; index++)
            {
                if (actual[index] != expected[index])
                {
                    return false;
                }
            }
            return true;
        }

        private static bool TryReadDigCancelTargets(
            JToken token,
            out List<DigCancelTarget> targets,
            out string error)
        {
            targets = new List<DigCancelTarget>();
            if (!(token is JArray array) || array.Count < 1 || array.Count > MaxDigCells)
            {
                error = "target array size is outside 1..32.";
                return false;
            }

            HashSet<int> cells = new HashSet<int>();
            HashSet<int> instances = new HashSet<int>();
            foreach (JToken item in array)
            {
                JObject target = item as JObject;
                if (target == null ||
                    !HasOnlyKeys(target, "cell", "designation_instance_id") ||
                    !TryReadInt(target, "cell", out int cell) ||
                    !TryReadInt(target, "designation_instance_id", out int instanceId) ||
                    cell < 0 ||
                    instanceId <= 0 ||
                    !cells.Add(cell) ||
                    !instances.Add(instanceId))
                {
                    error = "targets require unique non-negative cells and unique positive designation instance ids.";
                    return false;
                }

                targets.Add(new DigCancelTarget { Cell = cell, DesignationInstanceId = instanceId });
            }

            error = null;
            return true;
        }

        private static string ReadString(JObject obj, string key)
        {
            JToken token = obj?[key];
            return token == null || token.Type == JTokenType.Null ? null : token.ToString();
        }

        private sealed class PendingVerification
        {
            internal string Id;
            internal string Action;
            internal string Path;
            internal JObject Command;
            internal JObject BeforeState;
            internal string ExecutionMessage;
            internal List<GameObject> CreatedBlueprints;
            internal JObject AcceptedPolicy;
            internal long AcceptedSaveWriteGeneration;
            internal string AttemptId;
            internal string CommandIntentHash;
            internal long AcceptedAckSeq;
            internal long ExecutedAckSeq;
            internal float NextCheckAt;
            internal float Deadline;
        }

        private sealed class DigCancelTarget
        {
            internal int Cell;
            internal int DesignationInstanceId;
        }

        private sealed class BlueprintCancelTarget
        {
            internal int Cell;
            internal int BlueprintInstanceId;
        }

        private sealed class ValidationResult
        {
            internal bool IsValid;
            internal string AckStatus;
            internal string Reason;
            internal string Classification;
            internal JObject BeforeState;
            internal JObject AcceptedPolicy;
            internal long AcceptedSaveWriteGeneration;

            internal static ValidationResult Valid(
                JObject beforeState,
                JObject acceptedPolicy,
                long acceptedSaveWriteGeneration)
            {
                return new ValidationResult
                {
                    IsValid = true,
                    BeforeState = beforeState,
                    AcceptedPolicy = acceptedPolicy,
                    AcceptedSaveWriteGeneration = acceptedSaveWriteGeneration
                };
            }

            internal static ValidationResult Reject(string reason)
            {
                return new ValidationResult
                {
                    AckStatus = "rejected",
                    Reason = reason,
                    Classification = "behavior_fail"
                };
            }

            internal static ValidationResult Stale(string reason)
            {
                return new ValidationResult
                {
                    AckStatus = "skipped_stale",
                    Reason = reason,
                    Classification = "behavior_fail"
                };
            }
        }

        private sealed class CommandDirectories
        {
            internal CommandDirectories(string root)
            {
                Pending = Path.Combine(root, "pending");
                Approving = Path.Combine(root, "approving");
                Approved = Path.Combine(root, "approved");
                Executing = Path.Combine(root, "executing");
                Confirming = Path.Combine(root, "confirming");
                Done = Path.Combine(root, "done");
                Failed = Path.Combine(root, "failed");
                Rejected = Path.Combine(root, "rejected");
                Quarantine = Path.Combine(root, "quarantine");
                All = new[]
                {
                    Pending,
                    Approving,
                    Approved,
                    Executing,
                    Confirming,
                    Done,
                    Failed,
                    Rejected,
                    Quarantine
                };
            }

            internal string Pending { get; }
            internal string Approving { get; }
            internal string Approved { get; }
            internal string Executing { get; }
            internal string Confirming { get; }
            internal string Done { get; }
            internal string Failed { get; }
            internal string Rejected { get; }
            internal string Quarantine { get; }
            internal string[] All { get; }
        }
    }
}
