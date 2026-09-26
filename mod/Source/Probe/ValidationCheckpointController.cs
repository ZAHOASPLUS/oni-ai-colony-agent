using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    internal static class ValidationCheckpointController
    {
        private const float PollIntervalSeconds = 0.5f;
        private const float ReloadVerificationDelaySeconds = 0.75f;
        private const float ReloadVerificationTimeoutSeconds = 20f;
        private const int MaxControlIdLength = 80;
        private const string PolicyFileName = "validation-control-policy.json";
        private const string ReloadIntentFileName = "reload-intent.json";
        private const string ReloadRecoveryFailureCode = "corrupt_reload_intent";

        private static float nextPollAt;
        private static bool sessionActive;
        private static PendingReload pendingReload;

        internal static void BeginSession()
        {
            sessionActive = true;
            nextPollAt = 0f;
            RecoverReloadIntent();
        }

        internal static void EndSession()
        {
            sessionActive = false;
        }

        internal static void Update(float now)
        {
            if (!sessionActive)
            {
                return;
            }

            if (pendingReload != null)
            {
                VerifyPendingReload(now);
                return;
            }

            if (now < nextPollAt)
            {
                return;
            }

            nextPollAt = now + PollIntervalSeconds;
            PollApprovedControls(now);
        }

        private static void PollApprovedControls(float now)
        {
            try
            {
                ControlDirectories dirs = EnsureDirectories();
                string[] paths = Directory.GetFiles(dirs.Approved, "*.json");
                Array.Sort(paths, StringComparer.OrdinalIgnoreCase);
                foreach (string path in paths)
                {
                    ExecuteControl(path, dirs, now);
                    if (pendingReload != null)
                    {
                        return;
                    }
                }
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("validation_control_poll", ex);
            }
        }

        private static void ExecuteControl(string path, ControlDirectories dirs, float now)
        {
            JObject control = null;
            string id = Path.GetFileNameWithoutExtension(path);
            string operation = "unknown";
            string activeControlPath = path;
            try
            {
                control = JObject.Parse(File.ReadAllText(path, Encoding.UTF8));
                id = ReadString(control, "id") ?? id;
                operation = ReadString(control, "operation") ?? operation;
                string validationError = ValidateControl(control, path, dirs);
                if (validationError != null)
                {
                    EmitAck(id, operation, "rejected", validationError);
                    FinalizeControl(path, dirs.Quarantine, control, "rejected", validationError, null);
                    return;
                }

                control["status"] = "inflight";
                control["started_at"] = Timestamp();
                string inflightPath = MoveWithPayload(path, dirs.Inflight, control);
                activeControlPath = inflightPath;
                EmitAck(id, operation, "accepted", "Validation control policy and save identity matched.");

                if (operation == "save_checkpoint")
                {
                    ExecuteSaveCheckpoint(inflightPath, dirs, control);
                }
                else
                {
                    BeginReloadCheckpoint(inflightPath, dirs, control, now);
                }
            }
            catch (JsonException ex)
            {
                EmitAck(id, operation, "rejected", "Invalid validation control JSON: " + ex.Message);
                MoveRaw(path, dirs.Quarantine);
            }
            catch (Exception ex)
            {
                string reason = ex.GetBaseException().Message;
                if (operation == "reload_checkpoint")
                {
                    pendingReload = null;
                    DeleteReloadIntent();
                }
                EmitAck(id, operation, "failed", reason);
                if (control != null && File.Exists(activeControlPath))
                {
                    FinalizeControl(activeControlPath, dirs.Failed, control, "failed", reason, null);
                }
                else
                {
                    ProbeWriter.WriteError("validation_control_execute", ex);
                }
            }
        }

        private static string ValidateControl(JObject control, string path, ControlDirectories dirs)
        {
            string id = ReadString(control, "id");
            string operation = ReadString(control, "operation");
            if (!string.Equals(ReadString(control, "schema"), "oni.validation-control.v1", StringComparison.Ordinal))
            {
                return "Unsupported validation control schema.";
            }
            if (!IsSafeId(id) || !string.Equals(Path.GetFileNameWithoutExtension(path), id, StringComparison.Ordinal))
            {
                return "Validation control id is invalid or does not match the file name.";
            }
            if (HasProcessedId(dirs, id))
            {
                return "Validation control id was already processed.";
            }
            if (!string.Equals(ReadString(control, "status"), "approved", StringComparison.Ordinal) ||
                control["requires_approval"]?.Type != JTokenType.Boolean ||
                !control["requires_approval"].Value<bool>())
            {
                return "Approved controls require status=approved and requires_approval=true.";
            }
            if (operation != "save_checkpoint" && operation != "reload_checkpoint")
            {
                return "Unsupported validation control operation.";
            }
            if (!TryReadTimestamp(control, "created_at", out DateTimeOffset createdAt) ||
                !TryReadTimestamp(control, "approved_at", out DateTimeOffset approvedAt) ||
                !TryReadTimestamp(control, "expires_at", out DateTimeOffset expiresAt) ||
                expiresAt <= DateTimeOffset.Now ||
                approvedAt < createdAt ||
                approvedAt > expiresAt ||
                expiresAt - createdAt > TimeSpan.FromMinutes(2))
            {
                return "Validation control timestamps are missing, stale, or exceed the two-minute limit.";
            }
            if (!string.Equals(ReadString(control, "expected_mod_version"), Mod.ModVersion, StringComparison.Ordinal))
            {
                return "Validation control expected_mod_version does not match the loaded mod.";
            }
            if (!string.Equals(ReadString(control, "source_session_id"), ProbeSession.CurrentSessionId, StringComparison.Ordinal))
            {
                return "Validation control source_session_id is stale.";
            }
            string sourceSnapshotId = ReadString(control, "source_snapshot_id");
            if (string.IsNullOrEmpty(sourceSnapshotId) ||
                !sourceSnapshotId.StartsWith(ProbeSession.CurrentSessionId + ":", StringComparison.Ordinal))
            {
                return "Validation control source_snapshot_id is missing or stale.";
            }
            if (!string.Equals(ReadString(control, "save_id_hash"), ProbeSession.CurrentSaveIdHash, StringComparison.Ordinal) ||
                string.IsNullOrEmpty(ProbeSession.CurrentSaveIdHash))
            {
                return "Validation control save_id_hash does not match the active save.";
            }
            string activePath = ProbeSession.ReadActiveSavePath();
            string activePathId = ProbeSession.HashPath(activePath);
            if (!string.Equals(ReadString(control, "save_path_id"), activePathId, StringComparison.Ordinal) ||
                string.IsNullOrEmpty(activePathId))
            {
                return "Validation control save_path_id does not match the active save path.";
            }
            if (ProbeSession.IsSaveDirty)
            {
                return "The active save session is dirty; checkpoint controls require a verified loaded save.";
            }
            if (SpeedControlScreen.Instance == null || !SpeedControlScreen.Instance.IsPaused)
            {
                return "Checkpoint controls require the game to be paused.";
            }
            string queueError = ValidateActionQueueIsQuiescent();
            if (queueError != null)
            {
                return queueError;
            }

            JObject policy = LoadPolicy(out string policyError);
            if (policy == null)
            {
                return policyError;
            }
            if (!PolicyAllows(policy, control, operation, activePathId, out policyError))
            {
                return policyError;
            }

            JObject args = control["args"] as JObject;
            if (args == null)
            {
                return "Validation control args must be an object.";
            }
            if (operation == "reload_checkpoint")
            {
                return args.Count == 0 ? null : "reload_checkpoint does not accept arguments.";
            }
            if (!args.Properties().All(property =>
                    property.Name == "target_save_path" || property.Name == "target_save_path_id"))
            {
                return "save_checkpoint contains an unsupported argument.";
            }

            string targetPath = ReadString(args, "target_save_path");
            string targetPathId = ReadString(args, "target_save_path_id");
            if (string.IsNullOrEmpty(targetPath) && string.IsNullOrEmpty(targetPathId))
            {
                return null;
            }
            if (string.IsNullOrEmpty(targetPath) || string.IsNullOrEmpty(targetPathId))
            {
                return "save_checkpoint target path and path id must be supplied together.";
            }

            targetPath = ProbeSession.NormalizePath(targetPath);
            if (string.IsNullOrEmpty(targetPath) ||
                !string.Equals(Path.GetExtension(targetPath), ".sav", StringComparison.OrdinalIgnoreCase) ||
                !string.Equals(Path.GetDirectoryName(targetPath), Path.GetDirectoryName(activePath), StringComparison.OrdinalIgnoreCase) ||
                ProbeSession.PathsEqual(targetPath, activePath) ||
                File.Exists(targetPath) ||
                !string.Equals(ProbeSession.HashPath(targetPath), targetPathId, StringComparison.Ordinal) ||
                !ArrayContains(policy["allowed_target_save_path_ids"] as JArray, targetPathId))
            {
                return "save_checkpoint target must be a new .sav file beside the active save and explicitly whitelisted.";
            }
            return null;
        }

        private static bool PolicyAllows(
            JObject policy,
            JObject control,
            string operation,
            string activePathId,
            out string error)
        {
            if (!string.Equals(ReadString(policy, "schema"), "oni.validation-control-policy.v1", StringComparison.Ordinal) ||
                policy["controls_enabled"]?.Type != JTokenType.Boolean ||
                !policy["controls_enabled"].Value<bool>())
            {
                error = "Validation controls are disabled by local policy.";
                return false;
            }
            if (!TryReadTimestamp(policy, "expires_at", out DateTimeOffset expiresAt) || expiresAt <= DateTimeOffset.Now)
            {
                error = "Validation control policy is expired.";
                return false;
            }
            if (!string.Equals(ReadString(policy, "test_run_id"), ReadString(control, "test_run_id"), StringComparison.Ordinal) ||
                string.IsNullOrWhiteSpace(ReadString(control, "test_run_id")))
            {
                error = "Validation control test_run_id does not match policy.";
                return false;
            }
            if (!ArrayContains(policy["allowed_operations"] as JArray, operation) ||
                !ArrayContains(policy["allowed_save_hashes"] as JArray, ProbeSession.CurrentSaveIdHash) ||
                !ArrayContains(policy["allowed_save_path_ids"] as JArray, activePathId))
            {
                error = "Validation control operation or save identity is outside the policy whitelist.";
                return false;
            }
            error = null;
            return true;
        }

        private static string ValidateActionQueueIsQuiescent()
        {
            try
            {
                string root = Path.Combine(ProbeWriter.BridgeDirectory, "commands");
                foreach (string name in new[] { "pending", "approved" })
                {
                    string directory = Path.Combine(root, name);
                    if (Directory.Exists(directory) && Directory.GetFiles(directory, "*.json").Length > 0)
                    {
                        return "Checkpoint controls require empty action pending and approved queues.";
                    }
                }
                string policyPath = Path.Combine(ProbeWriter.BridgeDirectory, "action-policy.json");
                if (!File.Exists(policyPath))
                {
                    return "Action policy is missing.";
                }
                JObject actionPolicy = JObject.Parse(File.ReadAllText(policyPath, Encoding.UTF8));
                if (actionPolicy["actions_enabled"]?.Type != JTokenType.Boolean ||
                    actionPolicy["actions_enabled"].Value<bool>() ||
                    (actionPolicy["allowed_save_hashes"] as JArray)?.Count != 0)
                {
                    return "Checkpoint controls require the normal action policy to be disabled and unbound.";
                }
                return null;
            }
            catch (Exception ex)
            {
                return "Action queue quiescence check failed: " + ex.GetBaseException().Message;
            }
        }

        private static void ExecuteSaveCheckpoint(string inflightPath, ControlDirectories dirs, JObject control)
        {
            string activePath = ProbeSession.ReadActiveSavePath();
            JObject args = (JObject)control["args"];
            string requestedTarget = ReadString(args, "target_save_path");
            string targetPath = string.IsNullOrEmpty(requestedTarget)
                ? activePath
                : ProbeSession.NormalizePath(requestedTarget);
            string targetPathId = ProbeSession.HashPath(targetPath);
            string beforeHash = ProbeSession.CurrentSaveIdHash;
            string beforeSession = ProbeSession.CurrentSessionId;
            long beforeGeneration = ProbeSession.SaveWriteGeneration;

            ProbeWriter.WriteEvent("validation_control_started", new Dictionary<string, object>
            {
                { "control_id", ReadString(control, "id") },
                { "operation", "save_checkpoint" },
                { "test_run_id", ReadString(control, "test_run_id") },
                { "target_save_path_id", targetPathId }
            });

            if (SaveLoader.Instance == null)
            {
                throw new InvalidOperationException("SaveLoader.Instance is unavailable.");
            }
            string resultPath;
            bool transitionAuthorized = false;
            try
            {
                if (!string.Equals(
                        targetPathId,
                        ProbeSession.HashPath(activePath),
                        StringComparison.Ordinal))
                {
                    StartupBinding.AuthorizeSavePathTransition(targetPathId);
                    transitionAuthorized = true;
                }
                resultPath = SaveLoader.Instance.Save(targetPath, false, true);
            }
            finally
            {
                if (transitionAuthorized)
                {
                    StartupBinding.ClearAuthorizedSavePathTransition(targetPathId);
                }
            }
            ProbeSession.SaveWriteCompletion audit = ProbeSession.LastSaveWriteCompletion;
            string afterHash = ProbeSession.CurrentSaveIdHash;
            string afterPathId = ProbeSession.HashPath(ProbeSession.ReadActiveSavePath());
            bool success = audit != null &&
                audit.Generation > beforeGeneration &&
                audit.Success &&
                string.Equals(audit.RequestedPathId, targetPathId, StringComparison.Ordinal) &&
                string.Equals(audit.ResultPathId, targetPathId, StringComparison.Ordinal) &&
                string.Equals(audit.ActivePathId, targetPathId, StringComparison.Ordinal) &&
                string.Equals(ProbeSession.HashPath(resultPath), targetPathId, StringComparison.Ordinal) &&
                string.Equals(afterPathId, targetPathId, StringComparison.Ordinal) &&
                string.Equals(audit.ActiveSaveIdHash, afterHash, StringComparison.Ordinal) &&
                !string.IsNullOrEmpty(afterHash) &&
                !ProbeSession.IsSaveDirty;

            JObject result = new JObject
            {
                ["source_session_id"] = beforeSession,
                ["result_session_id"] = ProbeSession.CurrentSessionId,
                ["before_save_id_hash"] = beforeHash,
                ["written_save_id_hash"] = audit?.WrittenSaveIdHash,
                ["active_save_id_hash"] = afterHash,
                ["active_save_path_id"] = afterPathId,
                ["save_write_generation"] = audit?.Generation,
                ["file_rewrite_observed"] = audit?.FileRewriteObserved,
                ["save_file_readable"] = audit?.SaveFileReadable,
                ["failure_reason"] = audit?.FailureReason
            };
            if (!success)
            {
                string reason = "SaveLoader.Save did not produce a verified checkpoint rewrite.";
                EmitAck(ReadString(control, "id"), "save_checkpoint", "failed", reason);
                FinalizeControl(inflightPath, dirs.Failed, control, "failed", reason, result);
                return;
            }

            EmitAck(ReadString(control, "id"), "save_checkpoint", "verified", "Checkpoint save rewrite verified.");
            FinalizeControl(inflightPath, dirs.Done, control, "verified", "Checkpoint save rewrite verified.", result);
        }

        private static void BeginReloadCheckpoint(
            string inflightPath,
            ControlDirectories dirs,
            JObject control,
            float now)
        {
            string activePath = ProbeSession.ReadActiveSavePath();
            pendingReload = new PendingReload
            {
                Control = control,
                InflightPath = inflightPath,
                SourceSessionId = ProbeSession.CurrentSessionId,
                ExpectedSaveIdHash = ProbeSession.CurrentSaveIdHash,
                ExpectedSavePathId = ProbeSession.HashPath(activePath),
                NextCheckAt = now + ReloadVerificationDelaySeconds,
                Deadline = now + ReloadVerificationTimeoutSeconds
            };
            WriteReloadIntent(pendingReload);

            ProbeWriter.WriteEvent("validation_control_started", new Dictionary<string, object>
            {
                { "control_id", ReadString(control, "id") },
                { "operation", "reload_checkpoint" },
                { "test_run_id", ReadString(control, "test_run_id") },
                { "expected_save_path_id", pendingReload.ExpectedSavePathId },
                { "expected_save_id_hash", pendingReload.ExpectedSaveIdHash }
            });

            LoadingOverlay.Load(delegate
            {
                LoadScreen.DoLoad(activePath);
            });
        }

        private static void VerifyPendingReload(float now)
        {
            PendingReload pending = pendingReload;
            if (pending == null || now < pending.NextCheckAt)
            {
                return;
            }
            pending.NextCheckAt = now + 0.25f;

            bool sessionChanged = !string.Equals(
                ProbeSession.CurrentSessionId,
                pending.SourceSessionId,
                StringComparison.Ordinal);
            bool saveMatches = string.Equals(
                ProbeSession.CurrentSaveIdHash,
                pending.ExpectedSaveIdHash,
                StringComparison.Ordinal);
            bool pathMatches = string.Equals(
                ProbeSession.HashPath(ProbeSession.ReadActiveSavePath()),
                pending.ExpectedSavePathId,
                StringComparison.Ordinal);
            bool stateReadable = false;
            if (sessionChanged && saveMatches && pathMatches && !ProbeSession.IsSaveDirty)
            {
                try
                {
                    stateReadable = ProbeStateReader.ReadState() != null;
                }
                catch
                {
                    stateReadable = false;
                }
            }

            if (sessionChanged && saveMatches && pathMatches && stateReadable)
            {
                pendingReload = null;
                DeleteReloadIntent();
                JObject result = ReloadResult(pending);
                EmitAck(ReadString(pending.Control, "id"), "reload_checkpoint", "verified", "Checkpoint reload identity verified.");
                FinalizeControl(
                    pending.InflightPath,
                    EnsureDirectories().Done,
                    pending.Control,
                    "verified",
                    "Checkpoint reload identity verified.",
                    result);
                return;
            }

            if (now < pending.Deadline)
            {
                return;
            }

            string reason = "Reload verification timed out before session, save hash, path, and state became consistent.";
            pendingReload = null;
            DeleteReloadIntent();
            EmitAck(ReadString(pending.Control, "id"), "reload_checkpoint", "failed", reason);
            FinalizeControl(
                pending.InflightPath,
                EnsureDirectories().Failed,
                pending.Control,
                "failed",
                reason,
                ReloadResult(pending));
        }

        private static JObject ReloadResult(PendingReload pending)
        {
            return new JObject
            {
                ["source_session_id"] = pending?.SourceSessionId,
                ["result_session_id"] = ProbeSession.CurrentSessionId,
                ["expected_save_id_hash"] = pending?.ExpectedSaveIdHash,
                ["active_save_id_hash"] = ProbeSession.CurrentSaveIdHash,
                ["expected_save_path_id"] = pending?.ExpectedSavePathId,
                ["active_save_path_id"] = ProbeSession.HashPath(ProbeSession.ReadActiveSavePath()),
                ["save_dirty"] = ProbeSession.IsSaveDirty
            };
        }

        private static void RecoverReloadIntent()
        {
            if (pendingReload != null)
            {
                pendingReload.NextCheckAt = Time.unscaledTime + ReloadVerificationDelaySeconds;
                pendingReload.Deadline = Time.unscaledTime + ReloadVerificationTimeoutSeconds;
                return;
            }

            string intentPath = ReloadIntentPath();
            if (!File.Exists(intentPath))
            {
                return;
            }

            JObject intent = null;
            try
            {
                intent = JObject.Parse(File.ReadAllText(intentPath, Encoding.UTF8));
                string inflightFile = ReadString(intent, "inflight_file");
                string inflightPath = Path.Combine(EnsureDirectories().Inflight, inflightFile ?? string.Empty);
                if (!string.Equals(ReadString(intent, "schema"), "oni.validation-reload-intent.v1", StringComparison.Ordinal) ||
                    string.IsNullOrEmpty(inflightFile) ||
                    Path.GetFileName(inflightFile) != inflightFile ||
                    !File.Exists(inflightPath))
                {
                    throw new InvalidDataException("Reload intent does not reference a valid inflight control.");
                }
                JObject control = JObject.Parse(File.ReadAllText(inflightPath, Encoding.UTF8));
                string controlId = ReadString(control, "id");
                string inflightStem = Path.GetFileNameWithoutExtension(inflightFile);
                if (!string.Equals(ReadString(control, "schema"), "oni.validation-control.v1", StringComparison.Ordinal) ||
                    !string.Equals(ReadString(control, "operation"), "reload_checkpoint", StringComparison.Ordinal) ||
                    !string.Equals(ReadString(control, "status"), "inflight", StringComparison.Ordinal) ||
                    !IsSafeId(controlId) ||
                    (!string.Equals(inflightStem, controlId, StringComparison.Ordinal) &&
                     !inflightStem.StartsWith(controlId + "-", StringComparison.Ordinal)) ||
                    !string.Equals(ReadString(intent, "control_id"), controlId, StringComparison.Ordinal) ||
                    string.IsNullOrWhiteSpace(ReadString(intent, "source_session_id")) ||
                    !IsSha256Id(ReadString(intent, "expected_save_id_hash")) ||
                    !IsSha256Id(ReadString(intent, "expected_save_path_id")))
                {
                    throw new InvalidDataException("Reload intent and inflight control bindings are inconsistent.");
                }
                pendingReload = new PendingReload
                {
                    Control = control,
                    InflightPath = inflightPath,
                    SourceSessionId = ReadString(intent, "source_session_id"),
                    ExpectedSaveIdHash = ReadString(intent, "expected_save_id_hash"),
                    ExpectedSavePathId = ReadString(intent, "expected_save_path_id"),
                    NextCheckAt = Time.unscaledTime + ReloadVerificationDelaySeconds,
                    Deadline = Time.unscaledTime + ReloadVerificationTimeoutSeconds
                };
            }
            catch (Exception ex)
            {
                try
                {
                    FinalizeReloadRecoveryFailure(intentPath, intent, ex);
                }
                catch (Exception recoveryEx)
                {
                    pendingReload = null;
                    ProbeWriter.WriteError("validation_reload_recovery_finalize", recoveryEx);
                }
            }
        }

        private static void FinalizeReloadRecoveryFailure(string intentPath, JObject intent, Exception error)
        {
            pendingReload = null;
            ControlDirectories dirs = EnsureDirectories();
            string detail = error?.GetBaseException().Message ?? "Unknown reload intent recovery error.";
            string reason = "Reload intent recovery failed; the control was not replayed.";
            JArray terminalControls = new JArray();

            foreach (string inflightPath in RecoveryInflightPaths(intent, dirs))
            {
                terminalControls.Add(FinalizeOrQuarantineRecoveredControl(inflightPath, dirs, reason));
            }

            string rawIntentFile = QuarantineRecoverySource(intentPath, dirs.ReloadRecoveryQuarantine, "reload-intent");
            JObject recoveryRecord = new JObject
            {
                ["schema"] = "oni.validation-reload-recovery.v1",
                ["status"] = "quarantined",
                ["reason_code"] = ReloadRecoveryFailureCode,
                ["message"] = reason,
                ["error_detail"] = detail,
                ["replayed"] = false,
                ["completed_at"] = Timestamp(),
                ["intent_control_id"] = ReadString(intent, "control_id"),
                ["intent_inflight_file"] = ReadString(intent, "inflight_file"),
                ["raw_intent_file"] = rawIntentFile,
                ["terminal_controls"] = terminalControls
            };
            string recoveryPath = UniqueTarget(dirs.ReloadRecoveryQuarantine, "reload-recovery");
            WriteJsonAtomic(recoveryPath, recoveryRecord);

            ProbeWriter.WriteEvent("validation_reload_recovery_quarantined", new Dictionary<string, object>
            {
                { "reason_code", ReloadRecoveryFailureCode },
                { "message", reason },
                { "raw_intent_file", rawIntentFile },
                { "recovery_record", Path.GetFileName(recoveryPath) },
                { "terminal_control_count", terminalControls.Count }
            });
        }

        private static IEnumerable<string> RecoveryInflightPaths(JObject intent, ControlDirectories dirs)
        {
            // A readable intent has one authoritative binding. Even when its payload is
            // corrupt, quarantine only that file so unrelated inflight controls survive.
            string inflightFile = ReadString(intent, "inflight_file");
            if (!string.IsNullOrEmpty(inflightFile) &&
                string.Equals(Path.GetFileName(inflightFile), inflightFile, StringComparison.Ordinal) &&
                string.Equals(Path.GetExtension(inflightFile), ".json", StringComparison.OrdinalIgnoreCase))
            {
                string boundPath = Path.Combine(dirs.Inflight, inflightFile);
                if (File.Exists(boundPath))
                {
                    try
                    {
                        JObject boundControl = JObject.Parse(File.ReadAllText(boundPath, Encoding.UTF8));
                        if (!string.Equals(ReadString(boundControl, "operation"), "reload_checkpoint", StringComparison.Ordinal))
                        {
                            return Array.Empty<string>();
                        }
                    }
                    catch (JsonException)
                    {
                        // The intent is the only available identity for an unreadable
                        // bound payload, so preserve its raw bytes and quarantine it.
                    }
                    catch (IOException)
                    {
                        // Treat a transiently unreadable bound payload exactly like
                        // malformed JSON. It must never become eligible for replay.
                    }
                    catch (UnauthorizedAccessException)
                    {
                        // The intent is the only available identity for an unreadable
                        // bound payload, so preserve its raw bytes and quarantine it.
                    }
                    return new[] { boundPath };
                }
                return Array.Empty<string>();
            }

            // With an unreadable/ambiguous intent, only valid reload controls can be
            // identified as possible matches. Never consume save controls or replay any
            // command. A corrupt payload is only touched when explicitly bound above.
            List<string> matches = new List<string>();
            string[] candidates = Directory.GetFiles(dirs.Inflight, "*.json");
            Array.Sort(candidates, StringComparer.OrdinalIgnoreCase);
            foreach (string candidate in candidates)
            {
                try
                {
                    JObject control = JObject.Parse(File.ReadAllText(candidate, Encoding.UTF8));
                    if (string.Equals(ReadString(control, "schema"), "oni.validation-control.v1", StringComparison.Ordinal) &&
                        string.Equals(ReadString(control, "operation"), "reload_checkpoint", StringComparison.Ordinal) &&
                        string.Equals(ReadString(control, "status"), "inflight", StringComparison.Ordinal))
                    {
                        matches.Add(candidate);
                    }
                }
                catch (JsonException)
                {
                    // An unreadable inflight payload cannot prove that it is unrelated.
                    // Quarantine it so no ambiguous command can remain replayable.
                    matches.Add(candidate);
                }
                catch (IOException)
                {
                    matches.Add(candidate);
                }
                catch (UnauthorizedAccessException)
                {
                    // An unreadable inflight payload cannot prove that it is unrelated.
                    // Quarantine it so no ambiguous command can remain replayable.
                    matches.Add(candidate);
                }
            }
            return matches;
        }

        private static JObject FinalizeOrQuarantineRecoveredControl(
            string inflightPath,
            ControlDirectories dirs,
            string reason)
        {
            try
            {
                JObject control = JObject.Parse(File.ReadAllText(inflightPath, Encoding.UTF8));
                string id = ReadString(control, "id") ?? Path.GetFileNameWithoutExtension(inflightPath);
                string operation = ReadString(control, "operation") ?? "reload_checkpoint";
                string inflightStem = Path.GetFileNameWithoutExtension(inflightPath);
                bool validReloadControl =
                    string.Equals(ReadString(control, "schema"), "oni.validation-control.v1", StringComparison.Ordinal) &&
                    string.Equals(operation, "reload_checkpoint", StringComparison.Ordinal) &&
                    string.Equals(ReadString(control, "status"), "inflight", StringComparison.Ordinal) &&
                    IsSafeId(id) &&
                    (string.Equals(inflightStem, id, StringComparison.Ordinal) ||
                     inflightStem.StartsWith(id + "-", StringComparison.Ordinal));
                string status = validReloadControl ? "failed" : "quarantined";
                string targetDirectory = validReloadControl ? dirs.Failed : dirs.Quarantine;
                JObject result = new JObject
                {
                    ["reason_code"] = ReloadRecoveryFailureCode,
                    ["reload_intent_status"] = "quarantined",
                    ["replayed"] = false
                };
                EmitAck(id, operation, status, reason);
                string terminalPath = FinalizeControlWithPath(
                    inflightPath,
                    targetDirectory,
                    control,
                    status,
                    reason,
                    result);
                return new JObject
                {
                    ["control_id"] = id,
                    ["status"] = status,
                    ["terminal_file"] = Path.GetFileName(terminalPath)
                };
            }
            catch (Exception ex)
            {
                string rawFile = QuarantineRecoverySource(
                    inflightPath,
                    dirs.ReloadRecoveryQuarantine,
                    "inflight-" + Path.GetFileNameWithoutExtension(inflightPath));
                string fallbackId = Path.GetFileNameWithoutExtension(inflightPath);
                if (!IsSafeId(fallbackId))
                {
                    fallbackId = StableRecoveryId(Path.GetFileName(inflightPath));
                }
                JObject fallback = new JObject
                {
                    ["schema"] = "oni.validation-control-recovery.v1",
                    ["id"] = fallbackId,
                    ["operation"] = "reload_checkpoint",
                    ["status"] = "quarantined",
                    ["completed_at"] = Timestamp(),
                    ["result"] = new JObject
                    {
                        ["message"] = reason,
                        ["reason_code"] = ReloadRecoveryFailureCode,
                        ["replayed"] = false,
                        ["raw_inflight_file"] = rawFile,
                        ["parse_error"] = ex.GetBaseException().Message
                    }
                };
                string terminalPath = UniqueTarget(dirs.Quarantine, fallbackId);
                WriteJsonAtomic(terminalPath, fallback);
                EmitAck(fallbackId, "reload_checkpoint", "quarantined", reason);
                ProbeWriter.WriteEvent("validation_control_completed", new Dictionary<string, object>
                {
                    { "control_id", fallbackId },
                    { "operation", "reload_checkpoint" },
                    { "status", "quarantined" },
                    { "message", reason },
                    { "reason_code", ReloadRecoveryFailureCode }
                });
                return new JObject
                {
                    ["control_id"] = fallbackId,
                    ["status"] = "quarantined",
                    ["terminal_file"] = Path.GetFileName(terminalPath),
                    ["raw_inflight_file"] = rawFile
                };
            }
        }

        private static string StableRecoveryId(string value)
        {
            using (SHA256 sha256 = SHA256.Create())
            {
                byte[] digest = sha256.ComputeHash(Encoding.UTF8.GetBytes(value ?? string.Empty));
                return "reload-recovery-" + string.Concat(digest.Take(12).Select(item => item.ToString("x2", CultureInfo.InvariantCulture)));
            }
        }

        private static string QuarantineRecoverySource(string source, string targetDirectory, string stem)
        {
            Directory.CreateDirectory(targetDirectory);
            if (!File.Exists(source))
            {
                return null;
            }
            string target = UniqueTarget(targetDirectory, stem);
            File.Move(source, target);
            return Path.GetFileName(target);
        }

        private static void WriteReloadIntent(PendingReload pending)
        {
            JObject intent = new JObject
            {
                ["schema"] = "oni.validation-reload-intent.v1",
                ["control_id"] = ReadString(pending.Control, "id"),
                ["inflight_file"] = Path.GetFileName(pending.InflightPath),
                ["source_session_id"] = pending.SourceSessionId,
                ["expected_save_id_hash"] = pending.ExpectedSaveIdHash,
                ["expected_save_path_id"] = pending.ExpectedSavePathId,
                ["created_at"] = Timestamp()
            };
            WriteJsonAtomic(ReloadIntentPath(), intent);
        }

        private static void DeleteReloadIntent()
        {
            try
            {
                string path = ReloadIntentPath();
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("validation_reload_intent_delete", ex);
            }
        }

        private static JObject LoadPolicy(out string error)
        {
            string path = Path.Combine(ProbeWriter.BridgeDirectory, PolicyFileName);
            if (!File.Exists(path))
            {
                error = "Validation control policy is missing; controls default to disabled.";
                return null;
            }
            try
            {
                JObject policy = JObject.Parse(File.ReadAllText(path, Encoding.UTF8));
                error = null;
                return policy;
            }
            catch (Exception ex)
            {
                error = "Validation control policy is invalid: " + ex.GetBaseException().Message;
                return null;
            }
        }

        private static void FinalizeControl(
            string source,
            string targetDirectory,
            JObject control,
            string status,
            string message,
            JObject result)
        {
            FinalizeControlWithPath(source, targetDirectory, control, status, message, result);
        }

        private static string FinalizeControlWithPath(
            string source,
            string targetDirectory,
            JObject control,
            string status,
            string message,
            JObject result)
        {
            control["status"] = status;
            control["completed_at"] = Timestamp();
            control["result"] = result ?? new JObject();
            control["result"]["message"] = message;
            string target = MoveWithPayload(source, targetDirectory, control);
            ProbeWriter.WriteEvent("validation_control_completed", new Dictionary<string, object>
            {
                { "control_id", ReadString(control, "id") },
                { "operation", ReadString(control, "operation") },
                { "status", status },
                { "message", message },
                { "test_run_id", ReadString(control, "test_run_id") }
            });
            return target;
        }

        private static void EmitAck(string id, string operation, string status, string message)
        {
            ProbeWriter.WriteEvent("validation_control_ack", new Dictionary<string, object>
            {
                { "control_id", id },
                { "operation", operation },
                { "status", status },
                { "message", message }
            });
        }

        private static bool ArrayContains(JArray array, string expected)
        {
            return array != null && array.Any(token =>
                token.Type == JTokenType.String &&
                string.Equals(token.ToString(), expected, StringComparison.Ordinal));
        }

        private static bool HasProcessedId(ControlDirectories dirs, string id)
        {
            foreach (string directory in new[] { dirs.Inflight, dirs.Done, dirs.Failed, dirs.Quarantine })
            {
                if (!Directory.Exists(directory))
                {
                    continue;
                }
                if (Directory.GetFiles(directory, id + "*.json").Any(path =>
                    string.Equals(Path.GetFileNameWithoutExtension(path), id, StringComparison.Ordinal) ||
                    Path.GetFileNameWithoutExtension(path).StartsWith(id + "-", StringComparison.Ordinal)))
                {
                    return true;
                }
            }
            return false;
        }

        private static bool IsSafeId(string id)
        {
            return !string.IsNullOrWhiteSpace(id) &&
                id.Length <= MaxControlIdLength &&
                id.All(character => char.IsLetterOrDigit(character) || character == '-' || character == '_' || character == '.');
        }

        private static bool IsSha256Id(string value)
        {
            return value != null &&
                value.Length == 71 &&
                value.StartsWith("sha256:", StringComparison.Ordinal) &&
                value.Skip(7).All(character =>
                    (character >= '0' && character <= '9') ||
                    (character >= 'a' && character <= 'f'));
        }

        private static bool TryReadTimestamp(JObject value, string key, out DateTimeOffset result)
        {
            return DateTimeOffset.TryParse(
                ReadString(value, key),
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out result);
        }

        private static string ReadString(JObject value, string key)
        {
            JToken token = value?[key];
            return token == null || token.Type == JTokenType.Null ? null : token.ToString();
        }

        private static string MoveWithPayload(string source, string targetDirectory, JObject payload)
        {
            Directory.CreateDirectory(targetDirectory);
            string target = UniqueTarget(targetDirectory, Path.GetFileNameWithoutExtension(source));
            WriteJsonAtomic(target, payload);
            if (File.Exists(source))
            {
                File.Delete(source);
            }
            return target;
        }

        private static void MoveRaw(string source, string targetDirectory)
        {
            try
            {
                Directory.CreateDirectory(targetDirectory);
                if (File.Exists(source))
                {
                    File.Move(source, UniqueTarget(targetDirectory, Path.GetFileNameWithoutExtension(source)));
                }
            }
            catch (Exception ex)
            {
                ProbeWriter.WriteError("validation_control_move", ex);
            }
        }

        private static void WriteJsonAtomic(string target, JObject payload)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            string temp = target + ".tmp-" + Guid.NewGuid().ToString("N");
            File.WriteAllText(temp, payload.ToString(Formatting.Indented) + Environment.NewLine, new UTF8Encoding(false));
            if (File.Exists(target))
            {
                File.Delete(target);
            }
            File.Move(temp, target);
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
                stem + "-" + DateTimeOffset.Now.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture) +
                "-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".json");
        }

        private static ControlDirectories EnsureDirectories()
        {
            ControlDirectories dirs = new ControlDirectories(Path.Combine(ProbeWriter.BridgeDirectory, "validation-controls"));
            foreach (string directory in dirs.All)
            {
                Directory.CreateDirectory(directory);
            }
            return dirs;
        }

        private static string ReloadIntentPath()
        {
            return Path.Combine(ProbeWriter.BridgeDirectory, "validation-controls", ReloadIntentFileName);
        }

        private static string Timestamp()
        {
            return DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
        }

        private sealed class PendingReload
        {
            internal JObject Control;
            internal string InflightPath;
            internal string SourceSessionId;
            internal string ExpectedSaveIdHash;
            internal string ExpectedSavePathId;
            internal float NextCheckAt;
            internal float Deadline;
        }

        private sealed class ControlDirectories
        {
            internal ControlDirectories(string root)
            {
                Approved = Path.Combine(root, "approved");
                Inflight = Path.Combine(root, "inflight");
                Done = Path.Combine(root, "done");
                Failed = Path.Combine(root, "failed");
                Quarantine = Path.Combine(root, "quarantine");
                ReloadRecoveryQuarantine = Path.Combine(Quarantine, "reload-recovery");
                All = new[] { Approved, Inflight, Done, Failed, Quarantine, ReloadRecoveryQuarantine };
            }

            internal string Approved { get; }
            internal string Inflight { get; }
            internal string Done { get; }
            internal string Failed { get; }
            internal string Quarantine { get; }
            internal string ReloadRecoveryQuarantine { get; }
            internal string[] All { get; }
        }
    }
}
