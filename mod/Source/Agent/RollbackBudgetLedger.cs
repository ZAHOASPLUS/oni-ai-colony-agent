using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Newtonsoft.Json;

namespace OniAiProbeMod.Agent
{
    /// <summary>
    /// Trusted, game-side verification boundary. Never implement this by trusting model arguments,
    /// filenames, or a caller-supplied "is_autosave" flag. Verify the active colony and session,
    /// native autosave membership, source-save protection, and the selected slot's current identity.
    /// A slot that rotates must acquire a different opaque identity. Return l3_unavailable when
    /// any fact cannot be established, or no_autosave_available when no eligible native save exists.
    /// No production implementation is supplied until these facts have been verified in ONI.
    /// </summary>
    public interface IRollbackTargetVerifier
    {
        // Only a verified creation event may enroll a colony. Loading a save is NOT creation.
        RollbackVerification VerifyNewColony(string colonyId, string sessionId);
        RollbackVerification VerifyAutosave(string colonyId, string sessionId, string opaqueAutosaveId);
    }

    public sealed class RollbackVerification
    {
        private RollbackVerification(bool verified, double currentCycle, string code)
        {
            Verified = verified;
            CurrentCycle = currentCycle;
            Code = code;
        }

        public bool Verified { get; }
        public double CurrentCycle { get; }
        public string Code { get; }

        public static RollbackVerification Accept(double currentCycle)
        {
            return new RollbackVerification(true, currentCycle, "ok");
        }

        public static RollbackVerification Deny(string code = "l3_unavailable")
        {
            return new RollbackVerification(false, 0, code);
        }
    }

    public sealed class RollbackAttempt
    {
        internal RollbackAttempt(string attemptId, string requestId, string colonyId, string sessionId,
            string autosaveId, string recordedUtc, double cycle, string reason, string outcome, string resultCode)
        {
            AttemptId = attemptId;
            RequestId = requestId;
            ColonyId = colonyId;
            SessionId = sessionId;
            AutosaveId = autosaveId;
            RecordedUtc = recordedUtc;
            Cycle = cycle;
            Reason = reason;
            Outcome = outcome;
            ResultCode = resultCode;
        }

        public string AttemptId { get; }
        public string RequestId { get; }
        public string ColonyId { get; }
        public string SessionId { get; }
        public string AutosaveId { get; }
        public string RecordedUtc { get; }
        public double Cycle { get; }
        public string Reason { get; }
        public string Outcome { get; }
        public string ResultCode { get; }
    }

    public sealed class RollbackLedgerResult
    {
        internal RollbackLedgerResult(bool success, string code, int consumed = 0,
            RollbackAttempt attempt = null, bool loadAuthorized = false)
        {
            Success = success;
            Code = code;
            Consumed = consumed;
            Attempt = attempt;
            LoadAuthorized = loadAuthorized;
        }

        public bool Success { get; }
        public string Code { get; }
        public int Consumed { get; }
        public RollbackAttempt Attempt { get; }
        // True ONLY on a new, durably committed TryReserve; never on reads or outcome updates.
        public bool LoadAuthorized { get; }
    }

    /// <summary>
    /// Independent, fail-closed L3 accounting. This class never reads, copies, or loads game saves.
    /// The host must supply one stable, local user-level directory OUTSIDE save/clone trees and
    /// backups restored with them. Do not change it per launch, session, or model request.
    ///
    /// ProvisionNewStore is an explicit one-time installation step, NOT a recovery or startup
    /// fallback. Enrollment is allowed only for verified new-colony events. All ordinary calls
    /// require the existing store and permanent custody file. Missing/corrupt files, an incomplete
    /// write, or an unsupported atomic replacement disable L3; they never produce a new budget.
    ///
    /// TryReserve durably records pending + debit before it returns. Only its FIRST successful
    /// result authorizes ONE load attempt. Duplicate request IDs and pending records never
    /// authorize a retry. MarkOutcome is bookkeeping only; failure and abandonment never refund.
    /// The host must invalidate generation/queues, revalidate the exact native target immediately
    /// before loading, then observe fresh game state. If revalidation fails, keep the debit.
    ///
    /// Use on a local filesystem supporting FileShare.None and atomic File.Replace. Custody is
    /// held for each complete read-modify-write across instances/processes. A write-ahead revision
    /// watermark in the custody file detects an old restored ledger and partial commits. Interrupted
    /// commits can intentionally require manual recovery; do not automatically restore a backup.
    /// This is corruption/restart protection, not tamper-proof storage: deleting/restoring the
    /// ENTIRE directory cannot be detected without a separate trusted external authority. Do not
    /// auto-provision a missing directory. OS power-loss durability still depends on the filesystem.
    /// </summary>
    public sealed class RollbackBudgetLedger
    {
        public const string LedgerFileName = "rollback-ledger.json";
        public const string CustodyFileName = "rollback-ledger.custody";
        private const int SchemaVersion = 1;
        private const int MaxFileBytes = 16 * 1024 * 1024;
        private static readonly ConcurrentDictionary<string, object> Gates =
            new ConcurrentDictionary<string, object>(StringComparer.OrdinalIgnoreCase);
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
        private static readonly JsonSerializerSettings JsonSettings = new JsonSerializerSettings
        {
            TypeNameHandling = TypeNameHandling.None,
            MissingMemberHandling = MissingMemberHandling.Error,
            CheckAdditionalContent = true,
            DateParseHandling = DateParseHandling.None,
            MaxDepth = 32
        };
        private readonly string directory;
        private readonly string ledgerPath;
        private readonly string custodyPath;
        private readonly object gate;
        private readonly IRollbackTargetVerifier verifier;

        public RollbackBudgetLedger(string persistentDataDirectory, IRollbackTargetVerifier verifier)
        {
            if (string.IsNullOrWhiteSpace(persistentDataDirectory) || !Path.IsPathRooted(persistentDataDirectory))
                throw new ArgumentException("An absolute, stable user-level data directory is required.", nameof(persistentDataDirectory));
            directory = Path.GetFullPath(persistentDataDirectory);
            ledgerPath = Path.Combine(directory, LedgerFileName);
            custodyPath = Path.Combine(directory, CustodyFileName);
            gate = Gates.GetOrAdd(custodyPath, _ => new object());
            this.verifier = verifier;
        }

        public RollbackLedgerResult ProvisionNewStore()
        {
            if (!Monitor.TryEnter(gate)) return Fail("rollback_ledger_busy");
            try
            {
                Directory.CreateDirectory(directory);
                // CreateNew is essential: a missing JSON file must never reset an existing store.
                if (File.Exists(ledgerPath) || File.Exists(custodyPath)) return Fail("rollback_store_already_exists");
                using (var custody = new FileStream(custodyPath, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None))
                {
                    var data = new LedgerData { StoreId = Guid.NewGuid().ToString("N"), Revision = 0 };
                    WriteCustody(custody, data.StoreId, data.Revision);
                    WriteLedger(data, true);
                }
                return Ok();
            }
            catch (Exception ex) when (IsStorageFailure(ex)) { return Fail("rollback_ledger_write_failed"); }
            finally { Monitor.Exit(gate); }
        }

        public RollbackLedgerResult TryRegisterNewColony(string colonyId, string sessionId)
        {
            if (!IsOpaqueId(colonyId) || !IsOpaqueId(sessionId)) return Fail("invalid_rollback_identity");
            return WithStore((data, custody) =>
            {
                if (data.Colonies.ContainsKey(colonyId)) return Fail("rollback_colony_already_registered");
                var verification = Verify(() => verifier.VerifyNewColony(colonyId, sessionId));
                if (!verification.Verified) return Fail(verification.Code);
                data.Colonies.Add(colonyId, new ColonyData { CreatedSessionId = sessionId });
                Commit(data, custody);
                return Ok();
            });
        }

        public RollbackLedgerResult ReadBudget(string colonyId)
        {
            if (!IsOpaqueId(colonyId)) return Fail("invalid_rollback_identity");
            return WithStore((data, custody) => data.Colonies.TryGetValue(colonyId, out var colony)
                ? Ok(colony.Attempts.Count, colony.Attempts.Count == 0 ? null : ToPublic(colonyId, colony.Attempts[colony.Attempts.Count - 1]))
                : Fail("rollback_colony_not_registered"));
        }

        /// <param name="maxPerColony">Current player configuration, never model-controlled.</param>
        /// <param name="requestId">Stable unique ID for this request, reused on transport retries.</param>
        public RollbackLedgerResult TryReserve(string colonyId, string sessionId, string opaqueAutosaveId,
            string requestId, int maxPerColony, string reason)
        {
            if (!IsOpaqueId(colonyId) || !IsOpaqueId(sessionId) || !IsOpaqueId(opaqueAutosaveId) || !IsOpaqueId(requestId))
                return Fail("invalid_rollback_identity");
            if (maxPerColony < 0 || reason == null || reason.Length > 1024) return Fail("invalid_rollback_request");
            return WithStore((data, custody) =>
            {
                if (!data.Colonies.TryGetValue(colonyId, out var colony)) return Fail("rollback_colony_not_registered");
                foreach (var old in colony.Attempts)
                    if (old.RequestId == requestId) return Fail("rollback_attempt_already_recorded", colony.Attempts.Count);
                if (colony.Attempts.Count >= maxPerColony) return Fail("rollback_limit_exhausted", colony.Attempts.Count);
                var verification = Verify(() => verifier.VerifyAutosave(colonyId, sessionId, opaqueAutosaveId));
                if (!verification.Verified) return Fail(verification.Code, colony.Attempts.Count);
                var attempt = new AttemptData
                {
                    AttemptId = Guid.NewGuid().ToString("N"), RequestId = requestId,
                    SessionId = sessionId, AutosaveId = opaqueAutosaveId,
                    RecordedUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                    Cycle = verification.CurrentCycle, Reason = reason, Outcome = "pending", ResultCode = "pending"
                };
                colony.Attempts.Add(attempt);
                Commit(data, custody); // Never return a load receipt before both writes finish.
                return Ok(colony.Attempts.Count, ToPublic(colonyId, attempt), true);
            });
        }

        /// <summary>Record a terminal result, including after a restart. Never retries or refunds.</summary>
        public RollbackLedgerResult MarkOutcome(string colonyId, string attemptId, bool succeeded, string resultCode)
        {
            if (!IsOpaqueId(colonyId) || !IsOpaqueId(attemptId) || !IsOpaqueId(resultCode)) return Fail("invalid_rollback_request");
            return WithStore((data, custody) =>
            {
                if (!data.Colonies.TryGetValue(colonyId, out var colony)) return Fail("rollback_colony_not_registered");
                foreach (var attempt in colony.Attempts)
                {
                    if (attempt.AttemptId != attemptId) continue;
                    string outcome = succeeded ? "succeeded" : "failed";
                    if (attempt.Outcome != "pending")
                        return attempt.Outcome == outcome && attempt.ResultCode == resultCode
                            ? Ok(colony.Attempts.Count, ToPublic(colonyId, attempt))
                            : Fail("rollback_outcome_already_recorded", colony.Attempts.Count);
                    attempt.Outcome = outcome;
                    attempt.ResultCode = resultCode;
                    Commit(data, custody);
                    return Ok(colony.Attempts.Count, ToPublic(colonyId, attempt));
                }
                return Fail("rollback_attempt_not_found", colony.Attempts.Count);
            });
        }

        private RollbackLedgerResult WithStore(Func<LedgerData, FileStream, RollbackLedgerResult> operation)
        {
            if (!Monitor.TryEnter(gate)) return Fail("rollback_ledger_busy");
            try
            {
                if (!File.Exists(custodyPath) || !File.Exists(ledgerPath)) return Fail("rollback_ledger_missing");
                using (var custody = new FileStream(custodyPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    var watermark = ReadCustody(custody);
                    var data = ReadLedger();
                    if (watermark.StoreId != data.StoreId || watermark.Revision != data.Revision)
                        return Fail("rollback_ledger_revision_mismatch");
                    return operation(data, custody);
                }
            }
            catch (JsonException) { return Fail("rollback_ledger_corrupt"); }
            catch (InvalidDataException) { return Fail("rollback_ledger_corrupt"); }
            catch (Exception ex) when (IsStorageFailure(ex)) { return Fail("rollback_ledger_unavailable"); }
            finally { Monitor.Exit(gate); }
        }

        private RollbackVerification Verify(Func<RollbackVerification> verify)
        {
            if (verifier == null) return RollbackVerification.Deny();
            try
            {
                var result = verify();
                if (result == null) return RollbackVerification.Deny();
                if (!result.Verified) return RollbackVerification.Deny(IsOpaqueId(result.Code) ? result.Code : "l3_unavailable");
                return IsCycle(result.CurrentCycle) ? result : RollbackVerification.Deny("rollback_identity_unverified");
            }
            catch (Exception) { return RollbackVerification.Deny("rollback_verification_failed"); }
        }

        private void Commit(LedgerData data, FileStream custody)
        {
            checked { data.Revision++; }
            // Serialize and validate before advancing the watermark. If a later write fails,
            // mismatched revisions deliberately disable the store rather than resurrect credits.
            byte[] bytes = EncodeLedger(data);
            WriteCustody(custody, data.StoreId, data.Revision);
            WriteLedgerBytes(bytes, false);
        }

        private LedgerData ReadLedger()
        {
            using (var stream = new FileStream(ledgerPath, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (stream.Length <= 0 || stream.Length > MaxFileBytes) throw new InvalidDataException();
                using (var reader = new StreamReader(stream, Utf8, false))
                {
                    var envelope = JsonConvert.DeserializeObject<LedgerEnvelope>(reader.ReadToEnd(), JsonSettings);
                    if (envelope == null || envelope.Payload == null || envelope.Sha256 != Hash(envelope.Payload))
                        throw new InvalidDataException();
                    var data = JsonConvert.DeserializeObject<LedgerData>(envelope.Payload, JsonSettings);
                    Validate(data);
                    return data;
                }
            }
        }

        private static byte[] EncodeLedger(LedgerData data)
        {
            Validate(data);
            string payload = JsonConvert.SerializeObject(data, Formatting.None, JsonSettings);
            byte[] bytes = Utf8.GetBytes(JsonConvert.SerializeObject(new LedgerEnvelope
            { Payload = payload, Sha256 = Hash(payload) }, Formatting.Indented, JsonSettings));
            if (bytes.Length > MaxFileBytes) throw new InvalidDataException("Ledger capacity exceeded.");
            return bytes;
        }

        private void WriteLedger(LedgerData data, bool creating)
        {
            WriteLedgerBytes(EncodeLedger(data), creating);
        }

        private void WriteLedgerBytes(byte[] bytes, bool creating)
        {
            string temporaryPath = Path.Combine(directory, ".rollback-" + Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                using (var stream = new FileStream(temporaryPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }
                if (creating) File.Move(temporaryPath, ledgerPath);
                else File.Replace(temporaryPath, ledgerPath, null); // No non-atomic fallback.
            }
            finally
            {
                // A leftover temp file is never a source for recovery and never authorizes a load.
                try { if (File.Exists(temporaryPath)) File.Delete(temporaryPath); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }

        private static Watermark ReadCustody(FileStream stream)
        {
            if (stream.Length <= 0 || stream.Length > 1024) throw new InvalidDataException();
            stream.Position = 0;
            using (var reader = new StreamReader(stream, Utf8, false, 1024, true))
            {
                var watermark = JsonConvert.DeserializeObject<Watermark>(reader.ReadToEnd(), JsonSettings);
                if (watermark == null || !IsOpaqueId(watermark.StoreId) || watermark.Revision < 0)
                    throw new InvalidDataException();
                return watermark;
            }
        }

        private static void WriteCustody(FileStream stream, string storeId, long revision)
        {
            byte[] bytes = Utf8.GetBytes(JsonConvert.SerializeObject(new Watermark { StoreId = storeId, Revision = revision }));
            stream.Position = 0;
            stream.Write(bytes, 0, bytes.Length);
            stream.SetLength(bytes.Length);
            stream.Flush(true);
        }

        private static void Validate(LedgerData data)
        {
            if (data == null || data.Schema != SchemaVersion || !IsOpaqueId(data.StoreId) || data.Revision < 0 || data.Colonies == null)
                throw new InvalidDataException();
            var allAttempts = new HashSet<string>(StringComparer.Ordinal);
            foreach (var pair in data.Colonies)
            {
                var colony = pair.Value;
                if (!IsOpaqueId(pair.Key) || colony == null || !IsOpaqueId(colony.CreatedSessionId) || colony.Attempts == null)
                    throw new InvalidDataException();
                var requests = new HashSet<string>(StringComparer.Ordinal);
                foreach (var attempt in colony.Attempts)
                {
                    if (attempt == null || !IsOpaqueId(attempt.AttemptId) || !allAttempts.Add(attempt.AttemptId) ||
                        !IsOpaqueId(attempt.RequestId) || !requests.Add(attempt.RequestId) || !IsOpaqueId(attempt.SessionId) ||
                        !IsOpaqueId(attempt.AutosaveId) || !IsOpaqueId(attempt.ResultCode) || !IsCycle(attempt.Cycle) ||
                        attempt.Reason == null || attempt.Reason.Length > 1024 ||
                        !DateTimeOffset.TryParseExact(attempt.RecordedUtc, "O", CultureInfo.InvariantCulture, DateTimeStyles.None, out _) ||
                        (attempt.Outcome != "pending" && attempt.Outcome != "succeeded" && attempt.Outcome != "failed") ||
                        (attempt.Outcome == "pending" && attempt.ResultCode != "pending"))
                        throw new InvalidDataException();
                }
            }
        }

        private static bool IsCycle(double value) => !double.IsNaN(value) && !double.IsInfinity(value) && value >= 0;

        private static bool IsOpaqueId(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length > 128) return false;
            foreach (char c in value)
                if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_'))
                    return false;
            return true;
        }

        private static string Hash(string value)
        {
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Utf8.GetBytes(value))).Replace("-", "").ToLowerInvariant();
        }

        private static bool IsStorageFailure(Exception ex) => ex is IOException || ex is UnauthorizedAccessException ||
            ex is NotSupportedException || ex is System.Security.SecurityException || ex is DecoderFallbackException ||
            ex is EncoderFallbackException || ex is OverflowException;

        private static RollbackAttempt ToPublic(string colonyId, AttemptData data) => new RollbackAttempt(
            data.AttemptId, data.RequestId, colonyId, data.SessionId, data.AutosaveId, data.RecordedUtc, data.Cycle, data.Reason, data.Outcome, data.ResultCode);

        private static RollbackLedgerResult Ok(int consumed = 0, RollbackAttempt attempt = null, bool loadAuthorized = false) => new RollbackLedgerResult(true, "ok", consumed, attempt, loadAuthorized);
        private static RollbackLedgerResult Fail(string code, int consumed = 0) => new RollbackLedgerResult(false, code, consumed);

        [JsonObject(MemberSerialization.OptIn)]
        private sealed class LedgerEnvelope
        {
            [JsonProperty("payload", Required = Required.Always)] public string Payload { get; set; }
            [JsonProperty("sha256", Required = Required.Always)] public string Sha256 { get; set; }
        }

        [JsonObject(MemberSerialization.OptIn)]
        private sealed class Watermark
        {
            [JsonProperty("store_id", Required = Required.Always)] public string StoreId { get; set; }
            [JsonProperty("revision", Required = Required.Always)] public long Revision { get; set; }
        }

        [JsonObject(MemberSerialization.OptIn)]
        private sealed class LedgerData
        {
            [JsonProperty("schema", Required = Required.Always)] public int Schema { get; set; } = SchemaVersion;
            [JsonProperty("store_id", Required = Required.Always)] public string StoreId { get; set; }
            [JsonProperty("revision", Required = Required.Always)] public long Revision { get; set; }
            [JsonProperty("colonies", Required = Required.Always)] public Dictionary<string, ColonyData> Colonies { get; set; } = new Dictionary<string, ColonyData>(StringComparer.Ordinal);
        }

        [JsonObject(MemberSerialization.OptIn)]
        private sealed class ColonyData
        {
            [JsonProperty("created_session_id", Required = Required.Always)] public string CreatedSessionId { get; set; }
            [JsonProperty("attempts", Required = Required.Always)] public List<AttemptData> Attempts { get; set; } = new List<AttemptData>();
        }

        [JsonObject(MemberSerialization.OptIn)]
        private sealed class AttemptData
        {
            [JsonProperty("attempt_id", Required = Required.Always)] public string AttemptId { get; set; }
            [JsonProperty("request_id", Required = Required.Always)] public string RequestId { get; set; }
            [JsonProperty("session_id", Required = Required.Always)] public string SessionId { get; set; }
            [JsonProperty("autosave_id", Required = Required.Always)] public string AutosaveId { get; set; }
            [JsonProperty("recorded_utc", Required = Required.Always)] public string RecordedUtc { get; set; }
            [JsonProperty("cycle", Required = Required.Always)] public double Cycle { get; set; }
            [JsonProperty("reason", Required = Required.Always)] public string Reason { get; set; }
            [JsonProperty("outcome", Required = Required.Always)] public string Outcome { get; set; }
            [JsonProperty("result_code", Required = Required.Always)] public string ResultCode { get; set; }
        }
    }
}
