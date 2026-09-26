using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text;
using System.Threading;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    internal static class ProbeWriter
    {
        private static readonly object Sync = new object();
        private static readonly ConcurrentQueue<IDictionary<string, object>> PendingRecords =
            new ConcurrentQueue<IDictionary<string, object>>();
        private static readonly AutoResetEvent PendingSignal = new AutoResetEvent(false);
        private static Thread writerThread;
        private static volatile bool running;
        private static bool initialized;
        private static int pendingRecordCount;
        private static int safetyFault;
        private static long droppedSnapshotCount;
        private static long nonFiniteNumericCount;
        private static long writerFaultCount;
        private static string writerFaultCategory;
        private static long processSafetyFaultCount;
        private static string processSafetyFaultCategory;
        private const int MaxQueueDepth = 1000;
        private const int MaxJsonDepth = 64;
        private const int MaxJsonNodeCount = 1000000;
        // Consecutive sharing-violation retries tolerated before an open
        // failure becomes a process fault (about 10 s at the writer cadence).
        private const int MaxTransientOpenFailures = 40;
        private static int transientOpenFailures;

        internal static string BridgeDirectory { get; private set; }
        internal static string StateLogPath { get; private set; }
        internal static bool HasSafetyFault => Volatile.Read(ref safetyFault) != 0;

        internal static void Initialize()
        {
            lock (Sync)
            {
                if (initialized)
                {
                    return;
                }

                BridgeDirectory = ResolveBridgeDirectory();
                StateLogPath = Path.Combine(BridgeDirectory, "state.jsonl");
                Directory.CreateDirectory(BridgeDirectory);

                running = true;
                writerThread = new Thread(WriterLoop)
                {
                    IsBackground = true,
                    Name = "ONI AI Probe JSONL Writer"
                };
                writerThread.Start();
                initialized = true;
            }
        }

        internal static void Shutdown()
        {
            lock (Sync)
            {
                if (!initialized)
                {
                    return;
                }

                running = false;
                PendingSignal.Set();
            }

            try
            {
                writerThread?.Join(500);
            }
            catch
            {
                // Shutdown must never block the game.
            }
        }

        internal static long WriteEvent(string eventName)
        {
            return WriteEvent(eventName, null);
        }

        internal static long WriteEvent(string eventName, IDictionary<string, object> payload)
        {
            Dictionary<string, object> record = new Dictionary<string, object>
            {
                { "schema", "oni.event.v2" },
                { "event", eventName }
            };

            if (payload != null)
            {
                foreach (KeyValuePair<string, object> pair in payload)
                {
                    record[pair.Key] = pair.Value;
                }
            }

            WriteRecord(record);
            object sequence;
            if (record.TryGetValue("seq", out sequence))
            {
                try
                {
                    return Convert.ToInt64(sequence, CultureInfo.InvariantCulture);
                }
                catch
                {
                    // A missing sequence is fail-closed by command callers.
                }
            }
            return -1L;
        }

        internal static void WriteError(string stage, Exception ex)
        {
            WriteEvent("probe_error", new Dictionary<string, object>
            {
                { "stage", stage },
                { "category", "exception" },
                { "error_type", StableExceptionType(ex) }
            });
        }

        internal static void WriteError(string stage, string message)
        {
            WriteEvent("probe_error", new Dictionary<string, object>
            {
                { "stage", stage },
                { "message", message }
            });
        }

        internal static void WriteRecord(IDictionary<string, object> record)
        {
            bool queueSlotReserved = false;
            try
            {
                Initialize();
                if (!record.ContainsKey("ts"))
                {
                    record["ts"] = DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture);
                }

                ProbeSession.EnrichRecord(record);
                object eventName;
                bool isSnapshot =
                    record.TryGetValue("event", out eventName) &&
                    string.Equals(Convert.ToString(eventName, CultureInfo.InvariantCulture), "state_snapshot", StringComparison.Ordinal);
                if (!TryReserveQueueSlot())
                {
                    if (isSnapshot)
                    {
                        Interlocked.Increment(ref droppedSnapshotCount);
                    }
                    else
                    {
                        RegisterSafetyFault("queue_capacity_exceeded", true, true);
                    }
                    return;
                }
                queueSlotReserved = true;

                if (isSnapshot)
                {
                    record["writer"] = new Dictionary<string, object>
                    {
                        { "queue_depth", Math.Max(0, Volatile.Read(ref pendingRecordCount) - 1) },
                        { "dropped_snapshot_count", Interlocked.Read(ref droppedSnapshotCount) }
                    };
                }

                // Probe records contain only detached primitive/container state after
                // capture. Stream them on the writer thread so the game thread never
                // allocates a full snapshot-sized JSON string on the large object heap.
                PendingRecords.Enqueue(record);
                queueSlotReserved = false;
                PendingSignal.Set();
            }
            catch
            {
                if (queueSlotReserved)
                {
                    ReleaseQueueSlot();
                }
                RegisterSafetyFault("enqueue_failed", true, true);
            }
        }

        private static bool TryReserveQueueSlot()
        {
            while (true)
            {
                int observed = Volatile.Read(ref pendingRecordCount);
                if (observed >= MaxQueueDepth)
                {
                    return false;
                }
                if (Interlocked.CompareExchange(
                    ref pendingRecordCount,
                    observed + 1,
                    observed) == observed)
                {
                    return true;
                }
            }
        }

        private static void ReleaseQueueSlot()
        {
            while (true)
            {
                int observed = Volatile.Read(ref pendingRecordCount);
                if (observed <= 0)
                {
                    return;
                }
                if (Interlocked.CompareExchange(
                    ref pendingRecordCount,
                    observed - 1,
                    observed) == observed)
                {
                    return;
                }
            }
        }

        /// <summary>
        /// A Harmony lifecycle callback failed: session and save tracking are no
        /// longer trustworthy, so stop the controllers for the rest of the process.
        /// </summary>
        internal static void RegisterLifecycleFault()
        {
            RegisterSafetyFault("lifecycle_callback_failed", true, true);
        }

        private static long RegisterSafetyFault(
            string category,
            bool processSticky,
            bool stopControllersImmediately)
        {
            long faultCount;
            bool firstProcessFault = false;
            if (processSticky)
            {
                faultCount = Interlocked.Increment(ref processSafetyFaultCount);
                Interlocked.CompareExchange(ref processSafetyFaultCategory, category, null);
                firstProcessFault = Interlocked.Exchange(ref safetyFault, 1) == 0;
            }
            else
            {
                faultCount = Interlocked.Increment(ref writerFaultCount);
                if (!HasSafetyFault)
                {
                    Interlocked.Exchange(ref writerFaultCategory, category);
                }
            }

            if (firstProcessFault && stopControllersImmediately)
            {
                try
                {
                    Debug.LogError(Mod.LogPrefix + " Probe writer safety fault [" + category + "].");
                }
                catch
                {
                    // Native Unity logging is unavailable in the offline reflection harness.
                }
                ProbeRunner.StopControllers();
            }
            return faultCount;
        }

        private static string StableExceptionType(Exception ex)
        {
            if (ex is UnauthorizedAccessException)
            {
                return "unauthorized_access";
            }
            if (ex is InvalidDataException)
            {
                return "invalid_data";
            }
            if (ex is IOException)
            {
                return "io_error";
            }
            if (ex is ArgumentException)
            {
                return "invalid_argument";
            }
            if (ex is InvalidOperationException)
            {
                return "invalid_operation";
            }
            return ex == null ? "null_exception" : "unexpected_exception";
        }

        private static string ResolveBridgeDirectory()
        {
            string configured = Environment.GetEnvironmentVariable("ONI_AI_BRIDGE_DIR");
            if (!string.IsNullOrWhiteSpace(configured))
            {
                return configured;
            }

            string documents = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            return Path.Combine(documents, "Klei", "OxygenNotIncluded", "ai-bridge");
        }

        private static void WriterLoop()
        {
            while (running || (!PendingRecords.IsEmpty && !HasSafetyFault))
            {
                PendingSignal.WaitOne(250);
                FlushPendingRecords();
            }
        }

        private static void FlushPendingRecords()
        {
            if (string.IsNullOrEmpty(StateLogPath) || PendingRecords.IsEmpty)
            {
                return;
            }

            FileStream stream;
            try
            {
                stream = new FileStream(StateLogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
            }
            catch (IOException ex) when (IsSharingViolation(ex))
            {
                // The Bridge renames state.jsonl during rotation and copy tools
                // hold it briefly.  Nothing was dequeued, so no record is lost:
                // retry on the next cadence instead of faulting the whole
                // process on one transient race (code review 2026-09-26, L3).
                if (++transientOpenFailures >= MaxTransientOpenFailures)
                {
                    RegisterSafetyFault("append_failed", true, false);
                }
                return;
            }
            catch
            {
                RegisterSafetyFault("append_failed", true, false);
                return;
            }

            transientOpenFailures = 0;
            try
            {
                using (stream)
                using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
                {
                    while (PendingRecords.TryDequeue(out IDictionary<string, object> record))
                    {
                        ReleaseQueueSlot();
                        IDictionary<string, object> recordToWrite = PrepareRecordForWrite(record);
                        // Serialize the whole line first so a serialization
                        // failure cannot leave half a record in the log.
                        StringWriter line = new StringWriter(CultureInfo.InvariantCulture);
                        JsonLineWriter.Write(line, recordToWrite);
                        line.WriteLine();
                        writer.Write(line.ToString());
                    }
                }
            }
            catch
            {
                RegisterSafetyFault("append_failed", true, false);
            }
        }

        private static bool IsSharingViolation(IOException exception)
        {
            // ERROR_SHARING_VIOLATION (32) or ERROR_LOCK_VIOLATION (33); .NET and
            // Mono both carry the Win32 code in the low word of HResult.
            int code = exception.HResult & 0xFFFF;
            return code == 32 || code == 33;
        }

        private static IDictionary<string, object> PrepareRecordForWrite(
            IDictionary<string, object> record)
        {
            if (IsSessionStarted(record) && !HasSafetyFault)
            {
                Interlocked.Exchange(ref writerFaultCount, 0L);
                Interlocked.Exchange(ref nonFiniteNumericCount, 0L);
                Interlocked.Exchange(ref writerFaultCategory, null);
            }
            AttachWriterDiagnostics(record);
            int nodeCount = 0;
            string failure;
            HashSet<object> ancestors = new HashSet<object>(ReferenceIdentityComparer.Instance);
            if (TryValidateJsonValue(record, 0, ref nodeCount, ancestors, out failure))
            {
                return record;
            }

            long faultCount = RegisterSafetyFault("record_validation_failed", false, false);
            if (string.Equals(failure, "non_finite_number", StringComparison.Ordinal))
            {
                Interlocked.Increment(ref nonFiniteNumericCount);
            }
            return CreateWriterValidationError(record, failure, faultCount);
        }

        private static bool IsSessionStarted(IDictionary<string, object> record)
        {
            object schema;
            object eventName;
            return record.TryGetValue("schema", out schema) &&
                string.Equals(
                    Convert.ToString(schema, CultureInfo.InvariantCulture),
                    "oni.event.v2",
                    StringComparison.Ordinal) &&
                record.TryGetValue("event", out eventName) &&
                string.Equals(
                    Convert.ToString(eventName, CultureInfo.InvariantCulture),
                    "session_started",
                    StringComparison.Ordinal);
        }

        private static void AttachWriterDiagnostics(IDictionary<string, object> record)
        {
            object eventName;
            if (!record.TryGetValue("event", out eventName) ||
                !string.Equals(
                    Convert.ToString(eventName, CultureInfo.InvariantCulture),
                    "state_snapshot",
                    StringComparison.Ordinal))
            {
                return;
            }

            object writerValue;
            Dictionary<string, object> writerStatus =
                record.TryGetValue("writer", out writerValue)
                    ? writerValue as Dictionary<string, object>
                    : null;
            if (writerStatus == null)
            {
                writerStatus = new Dictionary<string, object>
                {
                    { "queue_depth", 0 },
                    { "dropped_snapshot_count", Interlocked.Read(ref droppedSnapshotCount) }
                };
                record["writer"] = writerStatus;
            }

            long processFaultCount = Interlocked.Read(ref processSafetyFaultCount);
            long faultCount = processFaultCount + Interlocked.Read(ref writerFaultCount);
            writerStatus["fault_count"] = faultCount;
            writerStatus["non_finite_numeric_count"] =
                Interlocked.Read(ref nonFiniteNumericCount);
            if (faultCount > 0)
            {
                writerStatus["fault"] = processFaultCount > 0
                    ? Volatile.Read(ref processSafetyFaultCategory) ?? "process_safety_fault"
                    : Volatile.Read(ref writerFaultCategory) ?? "writer_fault";
            }
        }

        private static IDictionary<string, object> CreateWriterValidationError(
            IDictionary<string, object> rejected,
            string failure,
            long faultCount)
        {
            return new Dictionary<string, object>
            {
                { "schema", "oni.event.v2" },
                { "event", "probe_error" },
                { "stage", "writer_validation" },
                { "message", "record_rejected_before_write" },
                { "reason", failure ?? "unknown_validation_failure" },
                { "fault_count", faultCount },
                { "non_finite_numeric_count", Interlocked.Read(ref nonFiniteNumericCount) },
                { "rejected_event", ReadString(rejected, "event", "unknown") },
                { "ts", ReadString(rejected, "ts", DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)) },
                { "session_id", ReadString(rejected, "session_id", "writer-invalid-session") },
                { "seq", ReadPositiveSequence(rejected) },
                { "game_build", ReadString(rejected, "game_build", "unknown") },
                { "mod_version", ReadString(rejected, "mod_version", Mod.ModVersion) },
                { "save_id_hash", ReadOptionalString(rejected, "save_id_hash") },
                { "save_dirty", ReadBoolean(rejected, "save_dirty") },
                { "world_id", ReadWorldId(rejected) },
                { "validation_run_id", ReadOptionalString(rejected, "validation_run_id") },
                { "startup_binding_hash", ReadOptionalString(rejected, "startup_binding_hash") },
                { "startup_intent_hash", ReadOptionalString(rejected, "startup_intent_hash") },
                { "runtime_config_evidence_sha256", ReadOptionalString(rejected, "runtime_config_evidence_sha256") },
                { "validation_process_id", ReadPositiveProcessId(rejected) },
                { "startup_binding_status", ReadString(rejected, "startup_binding_status", "invalid") },
                { "requested_save_path_id", ReadOptionalString(rejected, "requested_save_path_id") },
                { "active_save_path_id", ReadOptionalString(rejected, "active_save_path_id") }
            };
        }

        private static string ReadString(
            IDictionary<string, object> record,
            string key,
            string fallback)
        {
            object value;
            return record.TryGetValue(key, out value) && value is string text && !string.IsNullOrEmpty(text)
                ? text
                : fallback;
        }

        private static object ReadOptionalString(
            IDictionary<string, object> record,
            string key)
        {
            object value;
            return record.TryGetValue(key, out value) && value is string text
                ? (object)text
                : null;
        }

        private static long ReadPositiveSequence(IDictionary<string, object> record)
        {
            object value;
            if (record.TryGetValue("seq", out value))
            {
                if (value is long longValue && longValue > 0)
                {
                    return longValue;
                }
                if (value is int intValue && intValue > 0)
                {
                    return intValue;
                }
            }
            return 1L;
        }

        private static int ReadPositiveProcessId(IDictionary<string, object> record)
        {
            object value;
            if (record.TryGetValue("validation_process_id", out value))
            {
                if (value is int intValue && intValue > 0)
                {
                    return intValue;
                }
                if (value is long longValue && longValue > 0 && longValue <= int.MaxValue)
                {
                    return (int)longValue;
                }
            }
            return StartupBinding.ValidationProcessId;
        }

        private static bool ReadBoolean(
            IDictionary<string, object> record,
            string key)
        {
            object value;
            return record.TryGetValue(key, out value) && value is bool flag && flag;
        }

        private static int ReadWorldId(IDictionary<string, object> record)
        {
            object value;
            return record.TryGetValue("world_id", out value) && value is int worldId
                ? worldId
                : -1;
        }

        private static bool TryValidateJsonValue(
            object value,
            int depth,
            ref int nodeCount,
            HashSet<object> ancestors,
            out string failure)
        {
            failure = null;
            nodeCount++;
            if (nodeCount > MaxJsonNodeCount)
            {
                failure = "node_limit_exceeded";
                return false;
            }
            if (depth > MaxJsonDepth)
            {
                failure = "depth_limit_exceeded";
                return false;
            }
            if (value == null || value is string || value is bool || value is char)
            {
                return true;
            }

            Type valueType = value.GetType();
            if (valueType.IsEnum ||
                value is System.Guid ||
                value is System.DateTime ||
                value is System.DateTimeOffset ||
                value is byte[])
            {
                return true;
            }

            TypeCode typeCode = Type.GetTypeCode(valueType);
            if (typeCode == TypeCode.Single)
            {
                float number = (float)value;
                if (float.IsNaN(number) || float.IsInfinity(number))
                {
                    failure = "non_finite_number";
                    return false;
                }
                return true;
            }
            if (typeCode == TypeCode.Double)
            {
                double number = (double)value;
                if (double.IsNaN(number) || double.IsInfinity(number))
                {
                    failure = "non_finite_number";
                    return false;
                }
                return true;
            }
            if (typeCode == TypeCode.Byte ||
                typeCode == TypeCode.SByte ||
                typeCode == TypeCode.Int16 ||
                typeCode == TypeCode.UInt16 ||
                typeCode == TypeCode.Int32 ||
                typeCode == TypeCode.UInt32 ||
                typeCode == TypeCode.Int64 ||
                typeCode == TypeCode.UInt64 ||
                typeCode == TypeCode.Decimal)
            {
                return true;
            }

            if (value is IDictionary<string, object> typedDictionary)
            {
                return TryValidateTypedDictionary(
                    typedDictionary,
                    depth,
                    ref nodeCount,
                    ancestors,
                    out failure);
            }
            if (value is IDictionary dictionary)
            {
                return TryValidateUntypedDictionary(
                    dictionary,
                    depth,
                    ref nodeCount,
                    ancestors,
                    out failure);
            }
            if (value is IEnumerable enumerable)
            {
                return TryValidateArray(
                    enumerable,
                    depth,
                    ref nodeCount,
                    ancestors,
                    out failure);
            }

            failure = "unsupported_value_type";
            return false;
        }

        private static bool TryValidateTypedDictionary(
            IDictionary<string, object> dictionary,
            int depth,
            ref int nodeCount,
            HashSet<object> ancestors,
            out string failure)
        {
            failure = null;
            if (!ancestors.Add(dictionary))
            {
                failure = "reference_cycle";
                return false;
            }
            try
            {
                foreach (KeyValuePair<string, object> pair in dictionary)
                {
                    if (!TryValidateJsonValue(
                        pair.Value,
                        depth + 1,
                        ref nodeCount,
                        ancestors,
                        out failure))
                    {
                        return false;
                    }
                }
                return true;
            }
            catch
            {
                failure = "container_enumeration_failed";
                return false;
            }
            finally
            {
                ancestors.Remove(dictionary);
            }
        }

        private static bool TryValidateUntypedDictionary(
            IDictionary dictionary,
            int depth,
            ref int nodeCount,
            HashSet<object> ancestors,
            out string failure)
        {
            failure = null;
            if (!ancestors.Add(dictionary))
            {
                failure = "reference_cycle";
                return false;
            }
            try
            {
                foreach (DictionaryEntry entry in dictionary)
                {
                    Convert.ToString(entry.Key, CultureInfo.InvariantCulture);
                    if (!TryValidateJsonValue(
                        entry.Value,
                        depth + 1,
                        ref nodeCount,
                        ancestors,
                        out failure))
                    {
                        return false;
                    }
                }
                return true;
            }
            catch
            {
                failure = "container_enumeration_failed";
                return false;
            }
            finally
            {
                ancestors.Remove(dictionary);
            }
        }

        private static bool TryValidateArray(
            IEnumerable values,
            int depth,
            ref int nodeCount,
            HashSet<object> ancestors,
            out string failure)
        {
            failure = null;
            if (!ancestors.Add(values))
            {
                failure = "reference_cycle";
                return false;
            }
            try
            {
                foreach (object item in values)
                {
                    if (!TryValidateJsonValue(
                        item,
                        depth + 1,
                        ref nodeCount,
                        ancestors,
                        out failure))
                    {
                        return false;
                    }
                }
                return true;
            }
            catch
            {
                failure = "container_enumeration_failed";
                return false;
            }
            finally
            {
                ancestors.Remove(values);
            }
        }

        private sealed class ReferenceIdentityComparer : IEqualityComparer<object>
        {
            internal static readonly ReferenceIdentityComparer Instance =
                new ReferenceIdentityComparer();

            public new bool Equals(object left, object right)
            {
                return ReferenceEquals(left, right);
            }

            public int GetHashCode(object value)
            {
                return RuntimeHelpers.GetHashCode(value);
            }
        }

        private static class JsonLineWriter
        {
            internal static void Write(TextWriter writer, object value)
            {
                WriteValue(writer, value);
            }

            private static void WriteValue(TextWriter writer, object value)
            {
                if (value == null)
                {
                    writer.Write("null");
                    return;
                }

                if (value is string text)
                {
                    WriteString(writer, text);
                    return;
                }

                if (value is bool flag)
                {
                    writer.Write(flag ? "true" : "false");
                    return;
                }

                if (value is char character)
                {
                    WriteString(writer, character.ToString());
                    return;
                }

                Type valueType = value.GetType();
                if (valueType.IsEnum)
                {
                    WriteString(writer, Convert.ToString(value, CultureInfo.InvariantCulture));
                    return;
                }

                if (value is System.Guid guid)
                {
                    WriteString(writer, guid.ToString("D"));
                    return;
                }

                if (value is System.DateTime dateTime)
                {
                    WriteString(writer, dateTime.ToString("o", CultureInfo.InvariantCulture));
                    return;
                }

                if (value is System.DateTimeOffset dateTimeOffset)
                {
                    WriteString(writer, dateTimeOffset.ToString("o", CultureInfo.InvariantCulture));
                    return;
                }

                if (value is byte[] bytes)
                {
                    WriteString(writer, Convert.ToBase64String(bytes));
                    return;
                }

                if (value is IDictionary<string, object> typedDictionary)
                {
                    WriteDictionary(writer, typedDictionary);
                    return;
                }

                if (value is IDictionary dictionary)
                {
                    WriteUntypedDictionary(writer, dictionary);
                    return;
                }

                if (value is IEnumerable enumerable && !(value is byte[]))
                {
                    WriteArray(writer, enumerable);
                    return;
                }

                TypeCode typeCode = Type.GetTypeCode(valueType);
                if (typeCode == TypeCode.Single)
                {
                    float number = (float)value;
                    if (float.IsNaN(number) || float.IsInfinity(number))
                    {
                        Interlocked.Increment(ref nonFiniteNumericCount);
                        writer.Write("null");
                    }
                    else
                    {
                        writer.Write(number.ToString("R", CultureInfo.InvariantCulture));
                    }
                    return;
                }

                if (typeCode == TypeCode.Double)
                {
                    double number = (double)value;
                    if (double.IsNaN(number) || double.IsInfinity(number))
                    {
                        Interlocked.Increment(ref nonFiniteNumericCount);
                        writer.Write("null");
                    }
                    else
                    {
                        writer.Write(number.ToString("R", CultureInfo.InvariantCulture));
                    }
                    return;
                }

                if (typeCode == TypeCode.Byte ||
                    typeCode == TypeCode.SByte ||
                    typeCode == TypeCode.Int16 ||
                    typeCode == TypeCode.UInt16 ||
                    typeCode == TypeCode.Int32 ||
                    typeCode == TypeCode.UInt32 ||
                    typeCode == TypeCode.Int64 ||
                    typeCode == TypeCode.UInt64 ||
                    typeCode == TypeCode.Decimal)
                {
                    writer.Write(Convert.ToString(value, CultureInfo.InvariantCulture));
                    return;
                }

                WriteString(writer, value.ToString());
            }

            private static void WriteDictionary(TextWriter writer, IDictionary<string, object> dictionary)
            {
                writer.Write('{');
                bool first = true;
                foreach (KeyValuePair<string, object> pair in dictionary)
                {
                    if (!first)
                    {
                        writer.Write(',');
                    }

                    first = false;
                    WriteString(writer, pair.Key);
                    writer.Write(':');
                    WriteValue(writer, pair.Value);
                }

                writer.Write('}');
            }

            private static void WriteUntypedDictionary(TextWriter writer, IDictionary dictionary)
            {
                writer.Write('{');
                bool first = true;
                foreach (DictionaryEntry entry in dictionary)
                {
                    if (!first)
                    {
                        writer.Write(',');
                    }

                    first = false;
                    WriteString(writer, Convert.ToString(entry.Key, CultureInfo.InvariantCulture));
                    writer.Write(':');
                    WriteValue(writer, entry.Value);
                }

                writer.Write('}');
            }

            private static void WriteArray(TextWriter writer, IEnumerable values)
            {
                writer.Write('[');
                bool first = true;
                foreach (object item in values)
                {
                    if (!first)
                    {
                        writer.Write(',');
                    }

                    first = false;
                    WriteValue(writer, item);
                }

                writer.Write(']');
            }

            private static void WriteString(TextWriter writer, string value)
            {
                writer.Write('"');
                foreach (char ch in value ?? string.Empty)
                {
                    switch (ch)
                    {
                        case '\\':
                            writer.Write("\\\\");
                            break;
                        case '"':
                            writer.Write("\\\"");
                            break;
                        case '\b':
                            writer.Write("\\b");
                            break;
                        case '\f':
                            writer.Write("\\f");
                            break;
                        case '\n':
                            writer.Write("\\n");
                            break;
                        case '\r':
                            writer.Write("\\r");
                            break;
                        case '\t':
                            writer.Write("\\t");
                            break;
                        default:
                            if (ch < 32)
                            {
                                writer.Write("\\u");
                                writer.Write(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                            }
                            else
                            {
                                writer.Write(ch);
                            }

                            break;
                    }
                }

                writer.Write('"');
            }
        }
    }
}
