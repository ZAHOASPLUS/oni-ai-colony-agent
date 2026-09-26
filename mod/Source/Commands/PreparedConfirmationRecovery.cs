using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Security;

namespace OniAiProbeMod.Commands
{
    // Recognizes the narrow crash window where Bridge has durably published a
    // confirmation transaction and prepared confirming/<id>.json in place, but
    // has not yet atomically renamed that exact payload into done/.  Keeping this
    // gate independent of Unity lets the boundary be exercised offline.
    internal static class PreparedConfirmationRecovery
    {
        internal const string JournalSchema = "oni.action-outcomes-commit.v1";
        private const string OutcomesSchema = "oni.action-outcomes.v1";
        private const string OutcomeSchema = "oni.action-outcome.v1";

        internal static bool TryRecognize(
            string bridgeDirectory,
            JObject command,
            string expectedCommandIntentHash,
            out string error)
        {
            error = null;
            try
            {
                if (command == null ||
                    !string.Equals(ReadString(command, "status"), "confirmed", StringComparison.Ordinal) ||
                    !(command["confirmation"] is JObject confirmation))
                {
                    error = "The confirming command is not a prepared Bridge confirmation.";
                    return false;
                }

                string commandId = ReadString(command, "id");
                string action = ReadString(command, "action");
                string sessionId = ReadString(command, "session_id");
                string attemptId = ReadString(command, "attempt_id");
                string commandIntentHash = ReadString(command, "command_intent_hash");
                if (string.IsNullOrEmpty(commandId) ||
                    string.IsNullOrEmpty(action) ||
                    string.IsNullOrEmpty(sessionId) ||
                    string.IsNullOrEmpty(attemptId) ||
                    !IsSha256(commandIntentHash) ||
                    !string.Equals(commandIntentHash, expectedCommandIntentHash, StringComparison.Ordinal))
                {
                    error = "The prepared command identity or immutable intent hash is invalid.";
                    return false;
                }

                string claimError = ExecutionClaimStore.ValidateClaim(
                    command,
                    attemptId,
                    commandIntentHash);
                if (claimError != null)
                {
                    error = claimError;
                    return false;
                }

                if (!ValidateEmbeddedConfirmation(
                        command,
                        confirmation,
                        commandId,
                        action,
                        commandIntentHash,
                        out error))
                {
                    return false;
                }

                string journalPath = Path.Combine(bridgeDirectory, "action-outcomes-commit.json");
                if (!TryReadObject(journalPath, out JObject journal, out error) ||
                    !HasExactProperties(
                        journal,
                        "schema",
                        "outcomes_hash",
                        "session_id",
                        "root_certificate",
                        "session_certificate",
                        "staged_at",
                        "transaction_hash"))
                {
                    error = error ?? "The Bridge confirmation journal has an unexpected shape.";
                    return false;
                }

                string safeSession = SafeSessionName(sessionId);
                string expectedSessionCertificate = "sessions/" + safeSession + "/action-outcomes.json";
                string outcomesHash = ReadString(journal, "outcomes_hash");
                if (!string.Equals(ReadString(journal, "schema"), JournalSchema, StringComparison.Ordinal) ||
                    !string.Equals(ReadString(journal, "session_id"), sessionId, StringComparison.Ordinal) ||
                    !string.Equals(ReadString(journal, "root_certificate"), "action-outcomes.json", StringComparison.Ordinal) ||
                    !string.Equals(ReadString(journal, "session_certificate"), expectedSessionCertificate, StringComparison.Ordinal) ||
                    !HasAwareTimestamp(ReadString(journal, "staged_at")) ||
                    !IsSha256(outcomesHash) ||
                    !HashFieldMatches(journal, "transaction_hash"))
                {
                    error = "The Bridge confirmation journal is invalid or does not bind this session.";
                    return false;
                }

                string rootCertificatePath = Path.Combine(bridgeDirectory, "action-outcomes.json");
                string sessionCertificatePath = Path.Combine(
                    bridgeDirectory,
                    "sessions",
                    safeSession,
                    "action-outcomes.json");
                if (!TryReadObject(rootCertificatePath, out JObject rootDocument, out error) ||
                    !TryReadObject(sessionCertificatePath, out JObject sessionDocument, out error))
                {
                    return false;
                }
                if (!JToken.DeepEquals(rootDocument, sessionDocument))
                {
                    error = "The root and session confirmation certificates differ.";
                    return false;
                }
                if (!ValidateOutcomesDocument(
                        rootDocument,
                        sessionId,
                        outcomesHash,
                        commandId,
                        confirmation,
                        out error))
                {
                    return false;
                }

                return true;
            }
            catch (Exception ex)
            {
                error = "Prepared confirmation recovery validation failed: " +
                    ex.GetBaseException().Message;
                return false;
            }
        }

        // Directory.GetFiles and File.ReadAllText are not one atomic operation.
        // If Bridge completes the prepared rename between them, retain a witness
        // lock and let ActionExecutor's normal done-certificate poll validate the
        // target instead of misclassifying the vanished source as unknown.
        internal static bool WasRenamedToDone(
            string confirmingPath,
            string doneDirectory,
            out string commandId)
        {
            commandId = null;
            try
            {
                if (string.IsNullOrEmpty(confirmingPath) ||
                    string.IsNullOrEmpty(doneDirectory) ||
                    File.Exists(confirmingPath))
                {
                    return false;
                }
                string fileName = Path.GetFileName(confirmingPath);
                string donePath = Path.Combine(doneDirectory, fileName);
                if (!File.Exists(donePath))
                {
                    return false;
                }
                commandId = Path.GetFileNameWithoutExtension(fileName);
                return !string.IsNullOrEmpty(commandId);
            }
            catch
            {
                commandId = null;
                return false;
            }
        }

        private static bool ValidateEmbeddedConfirmation(
            JObject command,
            JObject confirmation,
            string commandId,
            string action,
            string commandIntentHash,
            out string error)
        {
            JArray blockers = confirmation["blockers"] as JArray;
            JObject witness = confirmation["witness"] as JObject;
            JObject result = command["result"] as JObject;
            if (!string.Equals(ReadString(confirmation, "schema"), OutcomeSchema, StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "command_id"), commandId, StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "action"), action, StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "command_intent_hash"), commandIntentHash, StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "command_location"), "confirming", StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "executor_status"), "executed_pending_snapshot", StringComparison.Ordinal) ||
                !string.Equals(ReadString(confirmation, "confirmation_status"), "snapshot_confirmed", StringComparison.Ordinal) ||
                blockers == null || blockers.Count != 0 ||
                witness == null || result == null ||
                !HasAwareTimestamp(ReadString(witness, "ts")) ||
                !string.Equals(ReadString(command, "confirmed_at"), ReadString(witness, "ts"), StringComparison.Ordinal) ||
                !string.Equals(ReadString(result, "attestation_hash"), ReadString(confirmation, "attestation_hash"), StringComparison.Ordinal) ||
                !HashFieldMatches(confirmation, "attestation_hash"))
            {
                error = "The embedded Bridge confirmation is incomplete or mismatched.";
                return false;
            }

            error = null;
            return true;
        }

        private static bool ValidateOutcomesDocument(
            JObject document,
            string sessionId,
            string expectedOutcomesHash,
            string commandId,
            JObject embeddedConfirmation,
            out string error)
        {
            JObject source = document["source"] as JObject;
            JArray outcomes = document["outcomes"] as JArray;
            if (!string.Equals(ReadString(document, "schema"), OutcomesSchema, StringComparison.Ordinal) ||
                source == null ||
                !string.Equals(ReadString(source, "session_id"), sessionId, StringComparison.Ordinal) ||
                outcomes == null ||
                !string.Equals(ReadString(document, "outcomes_hash"), expectedOutcomesHash, StringComparison.Ordinal) ||
                !HashFieldMatches(document, "outcomes_hash"))
            {
                error = "The published Bridge confirmation certificate is invalid.";
                return false;
            }

            HashSet<string> identifiers = new HashSet<string>(StringComparer.Ordinal);
            int exactMatches = 0;
            foreach (JToken token in outcomes)
            {
                JObject outcome = token as JObject;
                string identifier = ReadString(outcome, "command_id");
                JArray blockers = outcome?["blockers"] as JArray;
                string confirmationStatus = ReadString(outcome, "confirmation_status");
                if (outcome == null ||
                    !string.Equals(ReadString(outcome, "schema"), OutcomeSchema, StringComparison.Ordinal) ||
                    string.IsNullOrEmpty(identifier) ||
                    !identifiers.Add(identifier) ||
                    blockers == null || blockers.Any(item => item.Type != JTokenType.String) ||
                    !(string.Equals(confirmationStatus, "awaiting_trusted_snapshot", StringComparison.Ordinal) ||
                      string.Equals(confirmationStatus, "snapshot_confirmed", StringComparison.Ordinal) ||
                      string.Equals(confirmationStatus, "rejected", StringComparison.Ordinal)) ||
                    (string.Equals(confirmationStatus, "snapshot_confirmed", StringComparison.Ordinal) && blockers.Count != 0) ||
                    !HashFieldMatches(outcome, "attestation_hash"))
                {
                    error = "The published Bridge confirmation certificate contains an invalid outcome.";
                    return false;
                }

                if (string.Equals(identifier, commandId, StringComparison.Ordinal) &&
                    JToken.DeepEquals(outcome, embeddedConfirmation))
                {
                    exactMatches++;
                }
            }

            if (exactMatches != 1)
            {
                error = "The prepared command is not uniquely embedded in the published certificate.";
                return false;
            }

            error = null;
            return true;
        }

        private static bool HashFieldMatches(JObject value, string field)
        {
            string declared = ReadString(value, field);
            if (!IsSha256(declared))
            {
                return false;
            }
            JObject payload = (JObject)value.DeepClone();
            payload.Remove(field);
            return string.Equals(
                declared,
                CanonicalHashContract.Hash(payload),
                StringComparison.Ordinal);
        }

        private static bool TryReadObject(string path, out JObject value, out string error)
        {
            value = null;
            error = null;
            try
            {
                value = CanonicalHashContract.ParseObjectStrict(
                    File.ReadAllText(path, Encoding.UTF8));
                return true;
            }
            catch (Exception ex)
            {
                error = "Could not read a strict Bridge confirmation artifact " +
                    Path.GetFileName(path) + ": " + ex.GetBaseException().Message;
                return false;
            }
        }

        private static bool HasExactProperties(JObject value, params string[] names)
        {
            if (value == null || value.Count != names.Length)
            {
                return false;
            }
            HashSet<string> expected = new HashSet<string>(names, StringComparer.Ordinal);
            return value.Properties().All(property => expected.Contains(property.Name));
        }

        private static bool HasAwareTimestamp(string value)
        {
            if (string.IsNullOrEmpty(value) ||
                !DateTimeOffset.TryParse(
                    value,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind,
                    out DateTimeOffset ignored))
            {
                return false;
            }
            if (value.EndsWith("Z", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            return value.Length >= 6 &&
                (value[value.Length - 6] == '+' || value[value.Length - 6] == '-') &&
                value[value.Length - 3] == ':';
        }

        private static string SafeSessionName(string sessionId)
        {
            StringBuilder output = new StringBuilder();
            bool replacing = false;
            foreach (char current in sessionId)
            {
                bool allowed =
                    (current >= 'A' && current <= 'Z') ||
                    (current >= 'a' && current <= 'z') ||
                    (current >= '0' && current <= '9') ||
                    current == '.' || current == '_' || current == '-';
                if (allowed)
                {
                    output.Append(current);
                    replacing = false;
                }
                else if (!replacing)
                {
                    output.Append('_');
                    replacing = true;
                }
                if (output.Length >= 120)
                {
                    break;
                }
            }
            return output.Length == 0 ? "waiting" : output.ToString();
        }

        private static bool IsSha256(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length != 71 ||
                !value.StartsWith("sha256:", StringComparison.Ordinal))
            {
                return false;
            }
            for (int index = 7; index < value.Length; index++)
            {
                char current = value[index];
                if (!((current >= '0' && current <= '9') ||
                      (current >= 'a' && current <= 'f')))
                {
                    return false;
                }
            }
            return true;
        }

        private static string ReadString(JObject value, string field)
        {
            JToken token = value?[field];
            return token != null && token.Type == JTokenType.String
                ? token.Value<string>()
                : null;
        }
    }
}
