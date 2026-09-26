using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Security;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    internal static class StartupBinding
    {
        internal const string BindingSchema = "oni.runtime-startup-binding.v2";
        internal const string BindingFileName = "runtime-startup-binding.json";
        internal const int MaximumBindingBytes = 64 * 1024;

        private static readonly object Sync = new object();
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private static readonly Regex RunIdPattern = new Regex(
            "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex NoncePattern = new Regex(
            "^startup-[0-9a-f]{32}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex Sha256Pattern = new Regex(
            "^sha256:[0-9a-f]{64}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex RuntimeBootstrapTransactionPattern = new Regex(
            "^rtb-[0-9a-f]{24}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex ExplicitOffsetPattern = new Regex(
            "(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$",
            RegexOptions.CultureInvariant);
        private static readonly HashSet<string> ExactFields = new HashSet<string>(
            new[]
            {
                "schema",
                "validation_run_id",
                "startup_nonce",
                "startup_intent_hash",
                "runtime_config_evidence_sha256",
                "environment_manifest_sha256",
                "expected_game_build",
                "expected_mod_version",
                "expected_probe_dll_sha256",
                "runtime_bootstrap_result_path_id_lower",
                "runtime_bootstrap_result_sha256",
                "runtime_bootstrap_result_hash",
                "runtime_bootstrap_transaction_id",
                "installed_probe_tree_content_hash",
                "requested_loader_path_id_upper",
                "requested_loader_save_sha256",
                "requested_loader_save_length",
                "created_at",
                "document_hash"
            },
            StringComparer.Ordinal);

        private static readonly int ProcessId = ReadProcessId();
        private static bool initialized;
        private static bool terminalFailure;
        private static bool requestVerified;
        private static bool sessionVerified;
        private static bool verifiedMarkerWritten;
        private static bool failureMarkerWritten;
        private static string status = "uninitialized";
        private static string validationRunId;
        private static string startupNonce;
        private static string startupIntentHash;
        private static string runtimeConfigEvidenceSha256;
        private static string environmentManifestSha256;
        private static string expectedProbeDllSha256;
        private static string runtimeBootstrapResultPathIdLower;
        private static string runtimeBootstrapResultSha256;
        private static string runtimeBootstrapResultHash;
        private static string runtimeBootstrapTransactionId;
        private static string installedProbeTreeContentHash;
        private static string expectedRequestedPathId;
        private static string expectedRequestedSaveSha256;
        private static long expectedRequestedSaveLength;
        private static string bindingHash;
        private static string requestedSavePathId;
        private static string activeSavePathId;
        private static string authorizedSaveHash;
        private static long authorizedSaveLength = -1L;
        private static string authorizedTransitionPathId;

        internal static bool IsSessionVerified
        {
            get
            {
                lock (Sync)
                {
                    return initialized && sessionVerified && !terminalFailure &&
                        string.Equals(status, "verified", StringComparison.Ordinal);
                }
            }
        }

        internal static string ValidationRunId
        {
            get
            {
                lock (Sync)
                {
                    return validationRunId;
                }
            }
        }

        internal static string BindingHash
        {
            get
            {
                lock (Sync)
                {
                    return bindingHash;
                }
            }
        }

        internal static string StartupIntentHash
        {
            get
            {
                lock (Sync)
                {
                    return startupIntentHash;
                }
            }
        }

        internal static string RuntimeConfigEvidenceSha256
        {
            get
            {
                lock (Sync)
                {
                    return runtimeConfigEvidenceSha256;
                }
            }
        }

        internal static int ValidationProcessId => ProcessId;

        internal static string Status
        {
            get
            {
                lock (Sync)
                {
                    return status;
                }
            }
        }

        internal static string RequestedSavePathId
        {
            get
            {
                lock (Sync)
                {
                    return requestedSavePathId;
                }
            }
        }

        internal static string ActiveSavePathId
        {
            get
            {
                lock (Sync)
                {
                    return activeSavePathId;
                }
            }
        }

        internal static void Initialize(string bridgeDirectory)
        {
            lock (Sync)
            {
                if (initialized)
                {
                    return;
                }

                initialized = true;
                status = "invalid";
                string bindingPath = string.IsNullOrEmpty(bridgeDirectory)
                    ? null
                    : Path.Combine(bridgeDirectory, BindingFileName);
                if (string.IsNullOrEmpty(bindingPath) || !File.Exists(bindingPath))
                {
                    FailLocked("missing");
                    return;
                }

                try
                {
                    byte[] raw = ReadBoundedFile(bindingPath);
                    bindingHash = HashBytes(raw);
                    string json = StrictUtf8.GetString(raw);
                    JObject document = CanonicalHashContract.ParseObjectStrict(json);
                    ValidateAndFreezeLocked(document);
                    status = "pending_request";
                }
                catch
                {
                    ClearUntrustedFieldsLocked();
                    FailLocked("invalid");
                }
            }
        }

        internal static void ObserveRequestedSave(
            string observedPathId,
            string observedSaveHash,
            long? observedSaveLength)
        {
            lock (Sync)
            {
                if (requestedSavePathId == null)
                {
                    requestedSavePathId = observedPathId;
                }
                if (!initialized || terminalFailure)
                {
                    return;
                }
                string requiredPathId = sessionVerified
                    ? activeSavePathId
                    : expectedRequestedPathId;
                if (!string.Equals(observedPathId, requiredPathId, StringComparison.Ordinal))
                {
                    FailLocked("request_path_mismatch");
                    return;
                }
                string requiredHash = sessionVerified
                    ? authorizedSaveHash
                    : expectedRequestedSaveSha256;
                long requiredLength = sessionVerified
                    ? authorizedSaveLength
                    : expectedRequestedSaveLength;
                if (!string.Equals(observedSaveHash, requiredHash, StringComparison.Ordinal))
                {
                    FailLocked("request_hash_mismatch");
                    return;
                }
                if (!observedSaveLength.HasValue ||
                    observedSaveLength.Value != requiredLength)
                {
                    FailLocked("request_length_mismatch");
                    return;
                }

                requestVerified = true;
                if (!sessionVerified)
                {
                    status = "request_verified";
                }
            }
        }

        internal static void ObserveActiveSave(
            string observedPathId,
            string observedSaveHash,
            long? observedSaveLength)
        {
            lock (Sync)
            {
                if (activeSavePathId == null)
                {
                    activeSavePathId = observedPathId;
                }
                if (!initialized || terminalFailure)
                {
                    return;
                }
                if (!requestVerified)
                {
                    FailLocked("request_missing");
                    return;
                }
                string requiredPathId = sessionVerified
                    ? activeSavePathId
                    : expectedRequestedPathId;
                if (!string.Equals(observedPathId, requiredPathId, StringComparison.Ordinal))
                {
                    FailLocked("active_path_mismatch");
                    return;
                }
                string requiredHash = sessionVerified
                    ? authorizedSaveHash
                    : expectedRequestedSaveSha256;
                long requiredLength = sessionVerified
                    ? authorizedSaveLength
                    : expectedRequestedSaveLength;
                if (!string.Equals(observedSaveHash, requiredHash, StringComparison.Ordinal) ||
                    !observedSaveLength.HasValue ||
                    observedSaveLength.Value != requiredLength)
                {
                    FailLocked("active_hash_mismatch");
                    return;
                }

                requestVerified = false;
                authorizedSaveHash = observedSaveHash;
                authorizedSaveLength = observedSaveLength.Value;
                sessionVerified = true;
                status = "verified";
                WriteVerifiedMarkerLocked();
            }
        }

        internal static void CancelRequestedSave()
        {
            lock (Sync)
            {
                if (!initialized || terminalFailure)
                {
                    return;
                }
                requestVerified = false;
                if (!sessionVerified)
                {
                    status = "pending_request";
                }
            }
        }

        internal static void ObserveVerifiedSaveWrite(
            string observedActivePathId,
            string observedActiveSaveHash,
            long observedActiveSaveLength)
        {
            lock (Sync)
            {
                if (!initialized || terminalFailure || !sessionVerified)
                {
                    return;
                }
                bool transitionPending = !string.IsNullOrEmpty(authorizedTransitionPathId);
                string requiredPathId = transitionPending
                    ? authorizedTransitionPathId
                    : activeSavePathId;
                if (!string.Equals(observedActivePathId, requiredPathId, StringComparison.Ordinal))
                {
                    FailLocked(transitionPending
                        ? "save_transition_target_mismatch"
                        : "active_path_mismatch");
                    return;
                }
                if (!Sha256Pattern.IsMatch(observedActiveSaveHash ?? string.Empty) ||
                    observedActiveSaveLength < 0)
                {
                    FailLocked("active_hash_mismatch");
                    return;
                }

                activeSavePathId = observedActivePathId;
                authorizedSaveHash = observedActiveSaveHash;
                authorizedSaveLength = observedActiveSaveLength;
                authorizedTransitionPathId = null;
            }
        }

        internal static void AuthorizeSavePathTransition(string targetPathId)
        {
            lock (Sync)
            {
                if (!initialized || terminalFailure || !sessionVerified ||
                    !Sha256Pattern.IsMatch(targetPathId ?? string.Empty))
                {
                    FailLocked("save_transition_authorization_invalid");
                    throw new InvalidOperationException("Save path transition authorization is unavailable.");
                }
                if (!string.IsNullOrEmpty(authorizedTransitionPathId))
                {
                    FailLocked("save_transition_authorization_nested");
                    throw new InvalidOperationException("A save path transition is already authorized.");
                }
                if (string.Equals(targetPathId, activeSavePathId, StringComparison.Ordinal))
                {
                    FailLocked("save_transition_authorization_replay");
                    throw new InvalidOperationException("The active save path is not a path transition.");
                }

                authorizedTransitionPathId = targetPathId;
            }
        }

        internal static void ClearAuthorizedSavePathTransition(string targetPathId)
        {
            lock (Sync)
            {
                if (string.IsNullOrEmpty(authorizedTransitionPathId))
                {
                    return;
                }
                if (!string.Equals(targetPathId, authorizedTransitionPathId, StringComparison.Ordinal))
                {
                    FailLocked("save_transition_authorization_clear_mismatch");
                    authorizedTransitionPathId = null;
                    return;
                }

                authorizedTransitionPathId = null;
            }
        }

        private static void ValidateAndFreezeLocked(JObject document)
        {
            if (document == null || document.Count != ExactFields.Count ||
                document.Properties().Any(property => !ExactFields.Contains(property.Name)))
            {
                throw new InvalidDataException("Startup binding field set is invalid.");
            }

            string schema = RequireString(document, "schema");
            string candidateRunId = RequireString(document, "validation_run_id");
            string candidateNonce = RequireString(document, "startup_nonce");
            string candidateIntentHash = RequireSha256(document, "startup_intent_hash");
            string candidateRuntimeHash = RequireSha256(document, "runtime_config_evidence_sha256");
            string candidateManifestHash = RequireSha256(document, "environment_manifest_sha256");
            string expectedGameBuild = RequireString(document, "expected_game_build");
            string expectedModVersion = RequireString(document, "expected_mod_version");
            string candidateDllHash = RequireSha256(document, "expected_probe_dll_sha256");
            string candidateRuntimeBootstrapResultPathId = RequireSha256(
                document,
                "runtime_bootstrap_result_path_id_lower");
            string candidateRuntimeBootstrapResultSha256 = RequireSha256(
                document,
                "runtime_bootstrap_result_sha256");
            string candidateRuntimeBootstrapResultHash = RequireSha256(
                document,
                "runtime_bootstrap_result_hash");
            string candidateRuntimeBootstrapTransactionId = RequireString(
                document,
                "runtime_bootstrap_transaction_id");
            string candidateInstalledProbeTreeContentHash = RequireSha256(
                document,
                "installed_probe_tree_content_hash");
            string candidatePathId = RequireSha256(document, "requested_loader_path_id_upper");
            string candidateSaveHash = RequireSha256(document, "requested_loader_save_sha256");
            long candidateSaveLength = RequireNonNegativeInteger(document, "requested_loader_save_length");
            string createdAt = RequireString(document, "created_at");
            string candidateDocumentHash = RequireSha256(document, "document_hash");

            if (!string.Equals(schema, BindingSchema, StringComparison.Ordinal) ||
                !RunIdPattern.IsMatch(candidateRunId) ||
                !NoncePattern.IsMatch(candidateNonce) ||
                !RuntimeBootstrapTransactionPattern.IsMatch(
                    candidateRuntimeBootstrapTransactionId) ||
                !string.Equals(expectedGameBuild, ProbeSession.GameBuild, StringComparison.Ordinal) ||
                !string.Equals(expectedModVersion, Mod.ModVersion, StringComparison.Ordinal) ||
                !IsExplicitOffsetTimestamp(createdAt) ||
                !string.Equals(candidateDocumentHash, ComputeDocumentHash(document), StringComparison.Ordinal) ||
                !string.Equals(candidateDllHash, HashExecutingAssembly(), StringComparison.Ordinal))
            {
                throw new InvalidDataException("Startup binding value validation failed.");
            }

            validationRunId = candidateRunId;
            startupNonce = candidateNonce;
            startupIntentHash = candidateIntentHash;
            runtimeConfigEvidenceSha256 = candidateRuntimeHash;
            environmentManifestSha256 = candidateManifestHash;
            expectedProbeDllSha256 = candidateDllHash;
            runtimeBootstrapResultPathIdLower = candidateRuntimeBootstrapResultPathId;
            runtimeBootstrapResultSha256 = candidateRuntimeBootstrapResultSha256;
            runtimeBootstrapResultHash = candidateRuntimeBootstrapResultHash;
            runtimeBootstrapTransactionId = candidateRuntimeBootstrapTransactionId;
            installedProbeTreeContentHash = candidateInstalledProbeTreeContentHash;
            expectedRequestedPathId = candidatePathId;
            expectedRequestedSaveSha256 = candidateSaveHash;
            expectedRequestedSaveLength = candidateSaveLength;
        }

        private static byte[] ReadBoundedFile(string path)
        {
            using (FileStream stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read))
            {
                if (stream.Length <= 0 || stream.Length > MaximumBindingBytes)
                {
                    throw new InvalidDataException("Startup binding size is invalid.");
                }

                int length = checked((int)stream.Length);
                byte[] raw = new byte[length];
                int offset = 0;
                while (offset < raw.Length)
                {
                    int read = stream.Read(raw, offset, raw.Length - offset);
                    if (read <= 0)
                    {
                        throw new EndOfStreamException("Startup binding ended before its declared length.");
                    }
                    offset += read;
                }
                if (stream.ReadByte() != -1)
                {
                    throw new InvalidDataException("Startup binding changed while it was read.");
                }
                return raw;
            }
        }

        private static string RequireString(JObject document, string name)
        {
            JToken value = document[name];
            if (value == null || value.Type != JTokenType.String)
            {
                throw new InvalidDataException("Startup binding field is not a string.");
            }
            string text = value.Value<string>();
            if (string.IsNullOrEmpty(text) || text.Any(character => character > 0x7f))
            {
                throw new InvalidDataException("Startup binding string is empty or non-ASCII.");
            }
            return text;
        }

        private static string RequireSha256(JObject document, string name)
        {
            string value = RequireString(document, name);
            if (!Sha256Pattern.IsMatch(value))
            {
                throw new InvalidDataException("Startup binding SHA-256 field is invalid.");
            }
            return value;
        }

        private static long RequireNonNegativeInteger(JObject document, string name)
        {
            JToken value = document[name];
            if (value == null || value.Type != JTokenType.Integer)
            {
                throw new InvalidDataException("Startup binding length is not an integer.");
            }
            try
            {
                long result = value.Value<long>();
                if (result < 0)
                {
                    throw new InvalidDataException("Startup binding length is negative.");
                }
                return result;
            }
            catch (Exception error) when (!(error is InvalidDataException))
            {
                throw new InvalidDataException("Startup binding length is outside Int64.", error);
            }
        }

        private static bool IsExplicitOffsetTimestamp(string value)
        {
            return ExplicitOffsetPattern.IsMatch(value) &&
                DateTimeOffset.TryParse(
                    value,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind,
                    out DateTimeOffset _);
        }

        private static string ComputeDocumentHash(JObject document)
        {
            JObject material = new JObject();
            foreach (JProperty property in document.Properties()
                .Where(property => !string.Equals(property.Name, "document_hash", StringComparison.Ordinal))
                .OrderBy(property => property.Name, StringComparer.Ordinal))
            {
                material.Add(property.Name, property.Value.DeepClone());
            }
            byte[] encoded = StrictUtf8.GetBytes(material.ToString(Formatting.None));
            return HashBytes(encoded);
        }

        private static string HashExecutingAssembly()
        {
            string location = Assembly.GetExecutingAssembly().Location;
            if (string.IsNullOrEmpty(location) || !File.Exists(location))
            {
                throw new InvalidDataException("Executing Probe assembly is unavailable.");
            }
            using (FileStream stream = new FileStream(
                location,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete))
            using (SHA256 sha256 = SHA256.Create())
            {
                return "sha256:" + ToHex(sha256.ComputeHash(stream));
            }
        }

        private static string HashBytes(byte[] value)
        {
            using (SHA256 sha256 = SHA256.Create())
            {
                return "sha256:" + ToHex(sha256.ComputeHash(value));
            }
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder result = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                result.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            }
            return result.ToString();
        }

        private static int ReadProcessId()
        {
            int processId = Process.GetCurrentProcess().Id;
            if (processId <= 0)
            {
                throw new InvalidOperationException("Validation process id is not positive.");
            }
            return processId;
        }

        private static void ClearUntrustedFieldsLocked()
        {
            validationRunId = null;
            startupNonce = null;
            startupIntentHash = null;
            runtimeConfigEvidenceSha256 = null;
            environmentManifestSha256 = null;
            expectedProbeDllSha256 = null;
            runtimeBootstrapResultPathIdLower = null;
            runtimeBootstrapResultSha256 = null;
            runtimeBootstrapResultHash = null;
            runtimeBootstrapTransactionId = null;
            installedProbeTreeContentHash = null;
            expectedRequestedPathId = null;
            expectedRequestedSaveSha256 = null;
            expectedRequestedSaveLength = 0L;
            authorizedSaveHash = null;
            authorizedSaveLength = -1L;
            authorizedTransitionPathId = null;
            requestVerified = false;
            sessionVerified = false;
            authorizedTransitionPathId = null;
        }

        private static void FailLocked(string category)
        {
            terminalFailure = true;
            requestVerified = false;
            sessionVerified = false;
            status = category;
            WriteFailureMarkerLocked(category);
        }

        private static void WriteVerifiedMarkerLocked()
        {
            if (verifiedMarkerWritten)
            {
                return;
            }
            verifiedMarkerWritten = true;
            Debug.Log(
                Mod.LogPrefix + " STARTUP_BINDING_VERIFIED" +
                " validation_run_id=" + validationRunId +
                " startup_binding_hash=" + bindingHash +
                " startup_intent_hash=" + startupIntentHash +
                " validation_process_id=" + ProcessId.ToString(CultureInfo.InvariantCulture) +
                " requested_save_path_id=" + (requestedSavePathId ?? "null") +
                " active_save_path_id=" + (activeSavePathId ?? "null"));
        }

        private static void WriteFailureMarkerLocked(string category)
        {
            if (failureMarkerWritten)
            {
                return;
            }
            failureMarkerWritten = true;
            Debug.LogWarning(
                Mod.LogPrefix + " STARTUP_BINDING_FAILED" +
                " startup_binding_status=" + category +
                " validation_run_id=" + (validationRunId ?? "null") +
                " validation_process_id=" + ProcessId.ToString(CultureInfo.InvariantCulture));
        }
    }
}
