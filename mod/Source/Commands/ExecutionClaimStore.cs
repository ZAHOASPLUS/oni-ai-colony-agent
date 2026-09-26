using System;
using System.Globalization;
using System.IO;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Commands
{
    // Pure filesystem support for the executor's durable pre-dispatch claim.
    // This class deliberately has no Unity/ONI dependencies so crash boundaries
    // can be exercised by an offline harness.
    internal static class ExecutionClaimStore
    {
        internal const string ClaimSchema = "oni.execution-claim.v1";
        internal const string AsyncAckDurability = "async_writer_queue_unconfirmed";

        internal static bool TryClaimApproved(
            string approvedPath,
            string executingDirectory,
            JObject command,
            string attemptId,
            string commandIntentHash,
            DateTimeOffset claimedAt,
            out string executingPath,
            out string error)
        {
            executingPath = null;
            error = null;
            try
            {
                if (string.IsNullOrEmpty(approvedPath) || !File.Exists(approvedPath))
                {
                    error = "The approved command disappeared before it could be claimed.";
                    return false;
                }
                if (command == null || string.IsNullOrEmpty(attemptId) || string.IsNullOrEmpty(commandIntentHash))
                {
                    error = "The execution claim identity is incomplete.";
                    return false;
                }

                Directory.CreateDirectory(executingDirectory);
                string fileName = Path.GetFileName(approvedPath);
                executingPath = Path.Combine(executingDirectory, fileName);
                if (File.Exists(executingPath))
                {
                    error = "An execution claim already exists for this command file.";
                    return false;
                }

                string claimedAtText = claimedAt.ToString("o", CultureInfo.InvariantCulture);
                command["status"] = "executing";
                command["attempt_id"] = attemptId;
                command["command_intent_hash"] = commandIntentHash;
                command["execution_started_at"] = claimedAtText;
                command["execution_claim"] = new JObject
                {
                    ["schema"] = ClaimSchema,
                    ["attempt_id"] = attemptId,
                    ["command_intent_hash"] = commandIntentHash,
                    ["claimed_at"] = claimedAtText,
                    ["claim_payload_durability"] = "file_stream_flush_true",
                    ["directory_entry_durability"] = "atomic_move_no_directory_flush_barrier",
                    ["accepted_ack"] = new JObject
                    {
                        ["state"] = "not_enqueued",
                        ["seq"] = JValue.CreateNull(),
                        ["durability"] = "not_assigned"
                    },
                    ["dispatch"] = new JObject
                    {
                        ["state"] = "not_started"
                    }
                };

                // First durably replace the approved payload with its claimed
                // envelope, then atomically rename that exact file into executing/.
                // A crash between those operations leaves status=executing in
                // approved/ but cannot have crossed the dispatch boundary.
                if (!TryReplaceFileContents(approvedPath, command, out error))
                {
                    return false;
                }

                File.Move(approvedPath, executingPath);
                FlushExistingFile(executingPath);
                return true;
            }
            catch (Exception ex)
            {
                if (!string.IsNullOrEmpty(executingPath) &&
                    File.Exists(executingPath) &&
                    !File.Exists(approvedPath))
                {
                    // File.Move is atomic on the command volume. If the target is
                    // visible and the source is gone, the claim succeeded even if
                    // the best-effort post-rename flush reported an error.
                    return true;
                }
                error = "Execution claim persistence failed: " + ex.GetBaseException().Message;
                return false;
            }
        }

        internal static bool TryPersistExecuting(
            string executingPath,
            JObject command,
            string attemptId,
            string commandIntentHash,
            out string error)
        {
            error = ValidateClaim(command, attemptId, commandIntentHash);
            if (error != null)
            {
                return false;
            }
            if (string.IsNullOrEmpty(executingPath) || !File.Exists(executingPath))
            {
                error = "The durable executing claim disappeared before it could be updated.";
                return false;
            }
            return TryReplaceFileContents(executingPath, command, out error);
        }

        internal static bool TryTransitionExecuting(
            string executingPath,
            string targetDirectory,
            JObject command,
            string attemptId,
            string commandIntentHash,
            out string targetPath,
            out string error)
        {
            targetPath = null;
            error = ValidateClaim(command, attemptId, commandIntentHash);
            if (error != null)
            {
                return false;
            }
            try
            {
                if (string.IsNullOrEmpty(executingPath) || !File.Exists(executingPath))
                {
                    error = "The durable executing claim disappeared before terminal transition.";
                    return false;
                }
                Directory.CreateDirectory(targetDirectory);
                targetPath = Path.Combine(targetDirectory, Path.GetFileName(executingPath));
                if (File.Exists(targetPath))
                {
                    error = "The terminal command target already exists; the executing claim was retained.";
                    return false;
                }
                if (!TryReplaceFileContents(executingPath, command, out error))
                {
                    return false;
                }
                File.Move(executingPath, targetPath);
                FlushExistingFile(targetPath);
                return true;
            }
            catch (Exception ex)
            {
                if (!string.IsNullOrEmpty(targetPath) &&
                    File.Exists(targetPath) &&
                    !File.Exists(executingPath))
                {
                    return true;
                }
                error = "Executing claim transition failed: " + ex.GetBaseException().Message;
                return false;
            }
        }

        internal static string ValidateClaim(
            JObject command,
            string attemptId,
            string commandIntentHash)
        {
            if (command == null)
            {
                return "The execution claim payload is unavailable.";
            }
            JObject claim = command["execution_claim"] as JObject;
            if (claim == null ||
                !string.Equals((string)claim["schema"], ClaimSchema, StringComparison.Ordinal) ||
                !string.Equals((string)command["attempt_id"], attemptId, StringComparison.Ordinal) ||
                !string.Equals((string)claim["attempt_id"], attemptId, StringComparison.Ordinal) ||
                !string.Equals((string)command["command_intent_hash"], commandIntentHash, StringComparison.Ordinal) ||
                !string.Equals((string)claim["command_intent_hash"], commandIntentHash, StringComparison.Ordinal))
            {
                return "The execution claim identity or immutable intent hash changed.";
            }
            return null;
        }

        private static bool TryReplaceFileContents(string path, JObject payload, out string error)
        {
            string temp = path + ".tmp-" + Guid.NewGuid().ToString("N");
            string backup = path + ".backup-" + Guid.NewGuid().ToString("N");
            try
            {
                WriteJsonDurable(temp, payload);
                File.Replace(temp, path, backup);
                FlushExistingFile(path);
                TryDelete(backup);
                error = null;
                return true;
            }
            catch (Exception ex)
            {
                TryDelete(temp);
                error = "Durable command payload replacement failed: " + ex.GetBaseException().Message;
                return false;
            }
        }

        private static void WriteJsonDurable(string path, JObject payload)
        {
            byte[] bytes = new UTF8Encoding(false).GetBytes(
                payload.ToString(Formatting.Indented) + Environment.NewLine);
            using (FileStream stream = new FileStream(
                path,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                4096,
                FileOptions.WriteThrough))
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
        }

        private static void FlushExistingFile(string path)
        {
            using (FileStream stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.ReadWrite,
                FileShare.Read,
                4096,
                FileOptions.WriteThrough))
            {
                stream.Flush(true);
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (!string.IsNullOrEmpty(path) && File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
                // A backup/temp residue is visible to diagnostics but must not
                // turn an already durable state transition into a replay.
            }
        }
    }
}
