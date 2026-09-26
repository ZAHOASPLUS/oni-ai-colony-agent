using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace OniAiProbeMod.Probe
{
    internal static class ProbeSession
    {
        private static readonly object Sync = new object();
        private static string sessionId;
        private static long sequence;
        private static long snapshotSequence;
        private static string saveIdHash;
        private static string pendingSaveIdHash;
        private static int worldId = -1;
        private static bool saveSessionActive;
        private static bool saveDirty;
        private static long saveWriteGeneration;
        private static SaveWriteCompletion lastSaveWriteCompletion;

        internal sealed class SaveWriteContext
        {
            internal string RequestedPath;
            internal string RequestedPathId;
            internal string BeforeSaveIdHash;
            internal System.DateTime StartedUtc;
            internal bool ExistedBefore;
            internal long LengthBefore;
            internal System.DateTime LastWriteUtcBefore;
        }

        internal sealed class SaveWriteCompletion
        {
            internal long Generation;
            internal bool Success;
            internal bool FileRewriteObserved;
            internal bool SaveFileReadable;
            internal string FailureReason;
            internal string RequestedPathId;
            internal string ResultPathId;
            internal string ActivePathId;
            internal string BeforeSaveIdHash;
            internal string WrittenSaveIdHash;
            internal string ActiveSaveIdHash;
        }

        internal static SaveWriteCompletion LastSaveWriteCompletion
        {
            get
            {
                lock (Sync)
                {
                    return CloneSaveWriteCompletion(lastSaveWriteCompletion);
                }
            }
        }

        internal static long SaveWriteGeneration
        {
            get
            {
                lock (Sync)
                {
                    return saveWriteGeneration;
                }
            }
        }

        internal static string CurrentSessionId
        {
            get
            {
                lock (Sync)
                {
                    EnsureProcessSessionLocked();
                    return sessionId;
                }
            }
        }

        internal static string CurrentSaveIdHash
        {
            get
            {
                lock (Sync)
                {
                    return saveIdHash;
                }
            }
        }

        internal static int CurrentWorldId
        {
            get
            {
                lock (Sync)
                {
                    return worldId;
                }
            }
        }

        internal static bool IsSaveSessionActive
        {
            get
            {
                lock (Sync)
                {
                    return saveSessionActive;
                }
            }
        }

        internal static bool IsSaveDirty
        {
            get
            {
                lock (Sync)
                {
                    return saveDirty;
                }
            }
        }

        internal static string GameBuild =>
            "cl-" + KleiVersion.ChangeList.ToString(CultureInfo.InvariantCulture);

        internal static void InitializeProcess()
        {
            lock (Sync)
            {
                EnsureProcessSessionLocked();
            }
        }

        internal static void StartSaveSession()
        {
            string currentSaveHash;
            lock (Sync)
            {
                currentSaveHash = pendingSaveIdHash;
                pendingSaveIdHash = null;
            }
            string activePath = NormalizePath(ReadActiveSavePath());
            string activePathId = HashPath(activePath);
            string activeSaveHash;
            long activeSaveLength;
            if (!TryReadSaveFingerprint(activePath, out activeSaveHash, out activeSaveLength))
            {
                activeSaveHash = null;
            }
            StartupBinding.ObserveActiveSave(
                activePathId,
                activeSaveHash,
                activeSaveHash == null ? (long?)null : activeSaveLength);
            if (string.IsNullOrEmpty(currentSaveHash))
            {
                currentSaveHash = activeSaveHash;
            }
            int currentWorldId = ReadActiveWorldId();
            lock (Sync)
            {
                sessionId = CreateSessionId("session");
                sequence = 0;
                snapshotSequence = 0;
                saveIdHash = currentSaveHash;
                worldId = currentWorldId;
                saveSessionActive = true;
                saveDirty = false;
            }
        }

        internal static void PrepareSaveLoad(string path)
        {
            string normalizedPath = NormalizePath(path);
            string pathId = HashPath(normalizedPath);
            string hash;
            long length;
            long? observedLength = TryReadSaveFingerprint(normalizedPath, out hash, out length)
                ? (long?)length
                : null;
            StartupBinding.ObserveRequestedSave(pathId, hash, observedLength);
            lock (Sync)
            {
                pendingSaveIdHash = hash;
            }
        }

        internal static void CancelPreparedSaveLoad()
        {
            StartupBinding.CancelRequestedSave();
            lock (Sync)
            {
                pendingSaveIdHash = null;
            }
        }

        internal static SaveWriteContext BeginSaveWrite(string path)
        {
            string requestedPath = NormalizePath(path);
            SaveWriteContext context = new SaveWriteContext
            {
                RequestedPath = requestedPath,
                RequestedPathId = HashPath(requestedPath),
                StartedUtc = System.DateTime.UtcNow
            };

            lock (Sync)
            {
                context.BeforeSaveIdHash = saveIdHash;
                if (saveSessionActive)
                {
                    saveDirty = true;
                }
            }

            try
            {
                FileInfo file = new FileInfo(requestedPath);
                context.ExistedBefore = file.Exists;
                if (file.Exists)
                {
                    context.LengthBefore = file.Length;
                    context.LastWriteUtcBefore = file.LastWriteTimeUtc;
                }
            }
            catch
            {
                context.ExistedBefore = false;
            }

            return context;
        }

        internal static SaveWriteCompletion CompleteSaveWrite(
            SaveWriteContext context,
            string resultPath,
            bool updateSavePointer)
        {
            context = context ?? new SaveWriteContext
            {
                RequestedPath = null,
                RequestedPathId = null,
                BeforeSaveIdHash = CurrentSaveIdHash,
                StartedUtc = System.DateTime.UtcNow
            };

            string normalizedResult = NormalizePath(resultPath);
            string activePath = NormalizePath(ReadActiveSavePath());
            bool resultMatchesRequest = PathsEqual(context.RequestedPath, normalizedResult);
            bool resultExists = false;
            long resultLength = 0;
            System.DateTime resultLastWriteUtc = System.DateTime.MinValue;
            try
            {
                FileInfo resultFile = new FileInfo(normalizedResult);
                resultExists = resultFile.Exists;
                if (resultExists)
                {
                    resultLength = resultFile.Length;
                    resultLastWriteUtc = resultFile.LastWriteTimeUtc;
                }
            }
            catch
            {
                resultExists = false;
            }

            string writtenHash = resultExists ? ReadSaveIdHash(normalizedResult) : null;
            bool saveFileReadable = resultExists && IsReadableSaveFile(normalizedResult);
            bool fileRewriteObserved = resultExists &&
                (!context.ExistedBefore ||
                 resultLength != context.LengthBefore ||
                 resultLastWriteUtc > context.LastWriteUtcBefore ||
                 !string.Equals(writtenHash, context.BeforeSaveIdHash, StringComparison.Ordinal));
            bool activePointerMatches = !updateSavePointer || PathsEqual(activePath, normalizedResult);
            string activeHash;
            long activeLength;
            if (!TryReadSaveFingerprint(activePath, out activeHash, out activeLength))
            {
                activeHash = null;
                activeLength = -1L;
            }
            bool success = resultMatchesRequest &&
                resultExists &&
                !string.IsNullOrEmpty(writtenHash) &&
                saveFileReadable &&
                fileRewriteObserved &&
                activePointerMatches &&
                !string.IsNullOrEmpty(activeHash);

            string failureReason = null;
            if (!resultMatchesRequest)
            {
                failureReason = "result_path_mismatch";
            }
            else if (!resultExists)
            {
                failureReason = "result_file_missing";
            }
            else if (string.IsNullOrEmpty(writtenHash))
            {
                failureReason = "written_hash_unavailable";
            }
            else if (!saveFileReadable)
            {
                failureReason = "save_file_unreadable";
            }
            else if (!fileRewriteObserved)
            {
                failureReason = "file_rewrite_not_observed";
            }
            else if (!activePointerMatches)
            {
                failureReason = "active_pointer_mismatch";
            }
            else if (string.IsNullOrEmpty(activeHash))
            {
                failureReason = "active_hash_unavailable";
            }

            SaveWriteCompletion completion = new SaveWriteCompletion
            {
                Success = success,
                FileRewriteObserved = fileRewriteObserved,
                SaveFileReadable = saveFileReadable,
                FailureReason = failureReason,
                RequestedPathId = context.RequestedPathId,
                ResultPathId = HashPath(normalizedResult),
                ActivePathId = HashPath(activePath),
                BeforeSaveIdHash = context.BeforeSaveIdHash,
                WrittenSaveIdHash = writtenHash,
                ActiveSaveIdHash = activeHash
            };

            lock (Sync)
            {
                if (success && saveSessionActive)
                {
                    saveIdHash = activeHash;
                    saveDirty = false;
                }
                else if (saveSessionActive)
                {
                    saveDirty = true;
                }

                completion.Generation = ++saveWriteGeneration;
                lastSaveWriteCompletion = CloneSaveWriteCompletion(completion);
            }

            if (success)
            {
                StartupBinding.ObserveVerifiedSaveWrite(
                    HashPath(activePath),
                    activeHash,
                    activeLength);
            }

            return completion;
        }

        internal static void EndSaveSession()
        {
            lock (Sync)
            {
                saveSessionActive = false;
                sessionId = CreateSessionId("process");
                sequence = 0;
                snapshotSequence = 0;
                saveIdHash = null;
                worldId = -1;
                saveDirty = false;
            }
        }

        internal static void EnrichRecord(IDictionary<string, object> record)
        {
            lock (Sync)
            {
                EnsureProcessSessionLocked();
                if (saveSessionActive)
                {
                    int activeWorldId = ReadActiveWorldId();
                    if (activeWorldId >= 0)
                    {
                        worldId = activeWorldId;
                    }
                }

                long nextSequence = ++sequence;
                record["session_id"] = sessionId;
                record["seq"] = nextSequence;
                record["game_build"] = GameBuild;
                record["mod_version"] = Mod.ModVersion;
                record["save_id_hash"] = saveIdHash;
                record["save_dirty"] = saveDirty;
                record["world_id"] = worldId;
                record["validation_run_id"] = StartupBinding.ValidationRunId;
                record["startup_binding_hash"] = StartupBinding.BindingHash;
                record["startup_intent_hash"] = StartupBinding.StartupIntentHash;
                record["runtime_config_evidence_sha256"] = StartupBinding.RuntimeConfigEvidenceSha256;
                record["validation_process_id"] = StartupBinding.ValidationProcessId;
                record["startup_binding_status"] = StartupBinding.Status;
                if (!record.ContainsKey("requested_save_path_id"))
                {
                    record["requested_save_path_id"] = StartupBinding.RequestedSavePathId;
                }
                if (!record.ContainsKey("active_save_path_id"))
                {
                    record["active_save_path_id"] = StartupBinding.ActiveSavePathId;
                }

                object eventName;
                if (record.TryGetValue("event", out eventName) &&
                    string.Equals(Convert.ToString(eventName, CultureInfo.InvariantCulture), "state_snapshot", StringComparison.Ordinal))
                {
                    record["snapshot_seq"] = ++snapshotSequence;
                    record["snapshot_id"] = sessionId + ":" + nextSequence.ToString(CultureInfo.InvariantCulture);
                }
            }
        }

        private static void EnsureProcessSessionLocked()
        {
            if (!string.IsNullOrEmpty(sessionId))
            {
                return;
            }

            sessionId = CreateSessionId("process");
            sequence = 0;
            snapshotSequence = 0;
            saveIdHash = null;
            worldId = -1;
            saveSessionActive = false;
            saveDirty = false;
        }

        private static string CreateSessionId(string prefix)
        {
            return prefix + "-" +
                DateTimeOffset.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture) + "-" +
                Guid.NewGuid().ToString("N").Substring(0, 8);
        }

        private static int ReadActiveWorldId()
        {
            try
            {
                return ClusterManager.Instance == null ? -1 : ClusterManager.Instance.activeWorldId;
            }
            catch
            {
                return -1;
            }
        }

        internal static string ReadActiveSaveIdHash()
        {
            try
            {
                return ReadSaveIdHash(ReadActiveSavePath());
            }
            catch
            {
                return null;
            }
        }

        internal static string ReadActiveSavePath()
        {
            try
            {
                return SaveLoader.GetActiveSaveFilePath();
            }
            catch
            {
                return null;
            }
        }

        private static bool IsReadableSaveFile(string path)
        {
            try
            {
                return !string.IsNullOrEmpty(path) && SaveGame.GetFileInfo(path) != null;
            }
            catch
            {
                return false;
            }
        }

        internal static string NormalizePath(string path)
        {
            try
            {
                return string.IsNullOrWhiteSpace(path)
                    ? null
                    : Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            }
            catch
            {
                return null;
            }
        }

        internal static bool PathsEqual(string left, string right)
        {
            return !string.IsNullOrEmpty(left) &&
                !string.IsNullOrEmpty(right) &&
                string.Equals(left, right, StringComparison.OrdinalIgnoreCase);
        }

        internal static string HashPath(string path)
        {
            if (string.IsNullOrEmpty(path))
            {
                return null;
            }

            try
            {
                byte[] pathBytes = Encoding.UTF8.GetBytes(path.ToUpperInvariant());
                using (SHA256 sha256 = SHA256.Create())
                {
                    return "sha256:" + ToHex(sha256.ComputeHash(pathBytes));
                }
            }
            catch
            {
                return null;
            }
        }

        private static string ReadSaveIdHash(string path)
        {
            string hash;
            long length;
            return TryReadSaveFingerprint(path, out hash, out length) ? hash : null;
        }

        private static bool TryReadSaveFingerprint(
            string path,
            out string hash,
            out long length)
        {
            hash = null;
            length = -1L;
            SaveFingerprint first;
            SaveFingerprint second;
            if (!TryReadSaveFingerprintOnce(path, out first) ||
                !TryReadSaveFingerprintOnce(path, out second))
            {
                return false;
            }
            if (first.Length != second.Length ||
                first.CreationTimeUtcTicks != second.CreationTimeUtcTicks ||
                first.LastWriteTimeUtcTicks != second.LastWriteTimeUtcTicks ||
                !string.Equals(first.Hash, second.Hash, StringComparison.Ordinal))
            {
                return false;
            }

            hash = second.Hash;
            length = second.Length;
            return true;
        }

        private static bool TryReadSaveFingerprintOnce(
            string path,
            out SaveFingerprint fingerprint)
        {
            fingerprint = default(SaveFingerprint);
            try
            {
                if (string.IsNullOrEmpty(path))
                {
                    return false;
                }

                FileInfo before = new FileInfo(path);
                before.Refresh();
                if (!before.Exists || before.Length < 0)
                {
                    return false;
                }
                long lengthBefore = before.Length;
                long creationBefore = before.CreationTimeUtc.Ticks;
                long writeBefore = before.LastWriteTimeUtc.Ticks;
                byte[] digest;
                using (FileStream stream = new FileStream(
                    path,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.Read))
                using (SHA256 sha256 = SHA256.Create())
                {
                    if (stream.Length != lengthBefore)
                    {
                        return false;
                    }
                    digest = sha256.ComputeHash(stream);
                    if (stream.Length != lengthBefore || stream.Position != lengthBefore)
                    {
                        return false;
                    }
                }

                FileInfo after = new FileInfo(path);
                after.Refresh();
                if (!after.Exists ||
                    after.Length != lengthBefore ||
                    after.CreationTimeUtc.Ticks != creationBefore ||
                    after.LastWriteTimeUtc.Ticks != writeBefore)
                {
                    return false;
                }
                fingerprint = new SaveFingerprint
                {
                    Length = lengthBefore,
                    CreationTimeUtcTicks = creationBefore,
                    LastWriteTimeUtcTicks = writeBefore,
                    Hash = "sha256:" + ToHex(digest)
                };
                return true;
            }
            catch
            {
                fingerprint = default(SaveFingerprint);
                return false;
            }
        }

        private struct SaveFingerprint
        {
            internal long Length;
            internal long CreationTimeUtcTicks;
            internal long LastWriteTimeUtcTicks;
            internal string Hash;
        }

        private static SaveWriteCompletion CloneSaveWriteCompletion(SaveWriteCompletion source)
        {
            if (source == null)
            {
                return null;
            }

            return new SaveWriteCompletion
            {
                Generation = source.Generation,
                Success = source.Success,
                FileRewriteObserved = source.FileRewriteObserved,
                SaveFileReadable = source.SaveFileReadable,
                FailureReason = source.FailureReason,
                RequestedPathId = source.RequestedPathId,
                ResultPathId = source.ResultPathId,
                ActivePathId = source.ActivePathId,
                BeforeSaveIdHash = source.BeforeSaveIdHash,
                WrittenSaveIdHash = source.WrittenSaveIdHash,
                ActiveSaveIdHash = source.ActiveSaveIdHash
            };
        }

        private static string ToHex(byte[] bytes)
        {
            char[] chars = new char[bytes.Length * 2];
            const string alphabet = "0123456789abcdef";
            for (int i = 0; i < bytes.Length; i++)
            {
                chars[i * 2] = alphabet[bytes[i] >> 4];
                chars[i * 2 + 1] = alphabet[bytes[i] & 15];
            }

            return new string(chars);
        }
    }
}
