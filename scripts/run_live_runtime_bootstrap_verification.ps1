[CmdletBinding()]
param(
    [ValidateSet(2)]
    [int]$Rounds = 2,
    [ValidateRange(1, 25)]
    [double]$HeartbeatSeconds = 10,
    [string]$CanonicalReport,
    [string]$WorkerScript,
    [string]$VerifierScript,
    [string]$ManagerScript,
    [string]$PythonExecutable,
    [string[]]$AdditionalSourceFile = @(),
    [string]$TestFixtureRoundManifest,
    [string]$TestRoot,
    [ValidateRange(1, 3000)]
    [int]$TotalTimeoutSeconds = 900,
    [switch]$TestInjectJobCleanupFailure,
    [switch]$TestInjectPostPublishFailure,
    [switch]$TestInjectRollbackIdentitySwap,
    [switch]$TestInjectSourceAuditFailure,
    [switch]$TestInjectCanonicalAuditFailure,
    [switch]$TestInjectPublishLockReleaseFailure,
    [switch]$TestInjectSourceHandleReleaseFailure,
    [switch]$TestInjectResourceMonitorNestedRename,
    [switch]$TestInjectResourceMonitorEnumerationIoFailure,
    [switch]$TestInjectResourceMonitorTransientChurn,
    [switch]$TestInjectResourceMonitorPersistentChurn,
    [switch]$TestProbeSourceHandlesAfterTransaction,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
} catch {
    # The runner still writes only JSON if a host does not expose a console.
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($WorkerScript)) {
    $WorkerScript = Join-Path $PSScriptRoot "run_live_runtime_bootstrap_verification_worker.py"
}
if ([string]::IsNullOrWhiteSpace($VerifierScript)) {
    $VerifierScript = Join-Path $PSScriptRoot "verify_live_runtime_bootstrap.py"
}
if ([string]::IsNullOrWhiteSpace($ManagerScript)) {
    $ManagerScript = Join-Path $PSScriptRoot "manage_live_runtime_bootstrap.py"
}
if ([string]::IsNullOrWhiteSpace($CanonicalReport)) {
    $CanonicalReport = [IO.Path]::Combine($ProjectRoot, "reports", "live-runtime-bootstrap-verification.json")
}
$script:ExpectedRoundScenarioNamesSha256 = "c4298404bb2fb4d62fac6a0fdb2879ff9e927087c82ed85b676d0de8c748dd26"
$script:MaximumSnapshotBytes = 32MB
$script:MaximumStdoutBytes = 8MB
$script:MaximumStderrBytes = 8MB
$script:MaximumExecutionRootBytes = 64MB
$script:MaximumExecutionRootEntries = 10000
$script:MaximumExecutionRootScanAttempts = 16
$script:MinimumExecutionRootScanRetryMilliseconds = 4
$script:MaximumFailureTextChars = 4096
$script:TestResourceMonitorNestedRenameInjected = $false
$script:TestResourceMonitorEnumerationIoFailureInjected = $false
$script:TestResourceMonitorTransientChurnInjected = $false
$script:TestResourceMonitorPersistentChurnInjectionCount = 0L

if ($null -eq ("OniRunner.NativeFileIdentity" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;

namespace OniRunner {
    public sealed class FileIdentityResult {
        public string Identity { get; set; }
        public uint LinkCount { get; set; }
    }

    public static class NativeFileIdentity {
        private const uint FILE_READ_ATTRIBUTES = 0x80;
        private const uint FILE_SHARE_READ = 0x1;
        private const uint FILE_SHARE_WRITE = 0x2;
        private const uint FILE_SHARE_DELETE = 0x4;
        private const uint OPEN_EXISTING = 3;
        private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;

        [StructLayout(LayoutKind.Sequential)]
        private struct FILETIME {
            public uint Low;
            public uint High;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION {
            public uint FileAttributes;
            public FILETIME CreationTime;
            public FILETIME LastAccessTime;
            public FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
            uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION information);

        private static FileIdentityResult ToResult(BY_HANDLE_FILE_INFORMATION info) {
            return new FileIdentityResult {
                Identity = String.Format(
                    "{0:x8}:{1:x8}{2:x8}",
                    info.VolumeSerialNumber, info.FileIndexHigh, info.FileIndexLow),
                LinkCount = info.NumberOfLinks
            };
        }

        public static FileIdentityResult ReadHandle(SafeFileHandle handle) {
            if (handle == null || handle.IsInvalid || handle.IsClosed)
                throw new ObjectDisposedException("handle");
            BY_HANDLE_FILE_INFORMATION info;
            if (!GetFileInformationByHandle(handle, out info)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileInformationByHandle failed");
            }
            return ToResult(info);
        }

        public static FileIdentityResult Read(string path, bool directory) {
            uint flags = directory ? FILE_FLAG_BACKUP_SEMANTICS : 0;
            using (SafeFileHandle handle = CreateFileW(
                path, FILE_READ_ATTRIBUTES,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                IntPtr.Zero, OPEN_EXISTING, flags, IntPtr.Zero)) {
                if (handle.IsInvalid) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateFileW failed: " + path);
                }
                BY_HANDLE_FILE_INFORMATION info;
                if (!GetFileInformationByHandle(handle, out info)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileInformationByHandle failed: " + path);
                }
                return ToResult(info);
            }
        }
    }

    public sealed class KillOnCloseJob : IDisposable {
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private SafeFileHandle handle;

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS {
            public UInt64 ReadOperationCount;
            public UInt64 WriteOperationCount;
            public UInt64 OtherOperationCount;
            public UInt64 ReadTransferCount;
            public UInt64 WriteTransferCount;
            public UInt64 OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
            public Int64 PerProcessUserTimeLimit;
            public Int64 PerJobUserTimeLimit;
            public UInt32 LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public UInt32 ActiveProcessLimit;
            public UIntPtr Affinity;
            public UInt32 PriorityClass;
            public UInt32 SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION {
            public Int64 TotalUserTime;
            public Int64 TotalKernelTime;
            public Int64 ThisPeriodTotalUserTime;
            public Int64 ThisPeriodTotalKernelTime;
            public UInt32 TotalPageFaultCount;
            public UInt32 TotalProcesses;
            public UInt32 ActiveProcesses;
            public UInt32 TotalTerminatedProcesses;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateJobObjectW(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            SafeFileHandle job, int infoClass,
            ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION information, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            SafeFileHandle job, int infoClass,
            ref JOBOBJECT_BASIC_ACCOUNTING_INFORMATION information, uint length, IntPtr returnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(SafeFileHandle job, uint exitCode);

        public KillOnCloseJob() {
            handle = CreateJobObjectW(IntPtr.Zero, null);
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObjectW failed");
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if (!SetInformationJobObject(
                handle, 9, ref info,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)))) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "SetInformationJobObject failed");
            }
        }

        public void Assign(Process process) {
            if (process == null) throw new ArgumentNullException("process");
            if (!AssignProcessToJobObject(handle, process.Handle))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
        }

        public uint ActiveProcessCount() {
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info = new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
            if (!QueryInformationJobObject(
                handle, 1, ref info,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)), IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "QueryInformationJobObject failed");
            return info.ActiveProcesses;
        }

        public bool TerminateAndWait(int milliseconds) {
            if (ActiveProcessCount() == 0) return true;
            if (!TerminateJobObject(handle, 201))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TerminateJobObject failed");
            Stopwatch watch = Stopwatch.StartNew();
            while (watch.ElapsedMilliseconds < milliseconds) {
                if (ActiveProcessCount() == 0) return true;
                Thread.Sleep(10);
            }
            return ActiveProcessCount() == 0;
        }

        public void Dispose() {
            if (handle != null) {
                handle.Dispose();
                handle = null;
            }
        }
    }

    public sealed class BoundedWriteStream : Stream {
        private readonly Stream inner;
        private readonly long maximumBytes;
        private long observedBytes;
        private int limitExceeded;

        public BoundedWriteStream(Stream destination, long maximum) {
            if (destination == null) throw new ArgumentNullException("destination");
            if (!destination.CanWrite) throw new ArgumentException("destination is not writable", "destination");
            if (maximum < 1) throw new ArgumentOutOfRangeException("maximum");
            inner = destination;
            maximumBytes = maximum;
        }

        public long ObservedBytes { get { return Interlocked.Read(ref observedBytes); } }
        public bool LimitExceeded { get { return Volatile.Read(ref limitExceeded) != 0; } }

        public override bool CanRead { get { return false; } }
        public override bool CanSeek { get { return false; } }
        public override bool CanWrite { get { return true; } }
        public override long Length { get { throw new NotSupportedException(); } }
        public override long Position {
            get { throw new NotSupportedException(); }
            set { throw new NotSupportedException(); }
        }

        public override void Flush() { inner.Flush(); }
        public override int Read(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
        public override long Seek(long offset, SeekOrigin origin) { throw new NotSupportedException(); }
        public override void SetLength(long value) { throw new NotSupportedException(); }

        public override void Write(byte[] buffer, int offset, int count) {
            if (buffer == null) throw new ArgumentNullException("buffer");
            if (offset < 0 || count < 0 || offset + count > buffer.Length) throw new ArgumentOutOfRangeException();
            long after = Interlocked.Add(ref observedBytes, count);
            long before = after - count;
            long remaining = maximumBytes - before;
            int allowed = remaining <= 0 ? 0 : (int)Math.Min((long)count, remaining);
            if (allowed > 0) inner.Write(buffer, offset, allowed);
            if (allowed != count) {
                Interlocked.Exchange(ref limitExceeded, 1);
                throw new IOException("bounded stream byte limit exceeded");
            }
        }

        public override Task WriteAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) {
            try {
                cancellationToken.ThrowIfCancellationRequested();
                Write(buffer, offset, count);
                return Task.FromResult(0);
            } catch (Exception error) {
                TaskCompletionSource<object> completion = new TaskCompletionSource<object>();
                completion.SetException(error);
                return completion.Task;
            }
        }
    }

    public sealed class StrictJsonScanner {
        private const int MaximumDepth = 64;
        private const int MaximumNodes = 100000;
        private const int MaximumStringCharacters = 1048576;
        private readonly string text;
        private int index;
        private int nodeCount;

        private StrictJsonScanner(string value) {
            text = value;
            index = 0;
            nodeCount = 0;
        }

        public static void ValidateObject(string value) {
            StrictJsonScanner parser = new StrictJsonScanner(value);
            parser.SkipWhitespace();
            if (parser.Peek() != '{') throw new FormatException("JSON root is not an object");
            parser.ParseObject(1);
            parser.SkipWhitespace();
            if (parser.index != parser.text.Length) throw new FormatException("trailing JSON content");
        }

        private char Peek() {
            if (index >= text.Length) throw new FormatException("unexpected end of JSON");
            return text[index];
        }

        private void SkipWhitespace() {
            while (index < text.Length) {
                char c = text[index];
                if (c != ' ' && c != '\t' && c != '\r' && c != '\n') break;
                index++;
            }
        }

        private void Expect(char expected) {
            if (Peek() != expected) throw new FormatException("expected '" + expected + "' at " + index);
            index++;
        }

        private void CountNode() {
            nodeCount++;
            if (nodeCount > MaximumNodes) throw new FormatException("maximum JSON node count exceeded");
        }

        private void EnterContainer(int depth) {
            if (depth > MaximumDepth) throw new FormatException("maximum JSON nesting depth exceeded");
            CountNode();
        }

        private void ParseValue(int depth) {
            SkipWhitespace();
            char c = Peek();
            if (c == '{') ParseObject(depth);
            else if (c == '[') ParseArray(depth);
            else if (c == '"') { CountNode(); ParseString(); }
            else if (c == 't') { CountNode(); ParseLiteral("true"); }
            else if (c == 'f') { CountNode(); ParseLiteral("false"); }
            else if (c == 'n') { CountNode(); ParseLiteral("null"); }
            else if (c == '-' || (c >= '0' && c <= '9')) { CountNode(); ParseNumber(); }
            else throw new FormatException("invalid JSON value at " + index);
        }

        private void ParseObject(int depth) {
            EnterContainer(depth);
            Expect('{');
            SkipWhitespace();
            HashSet<string> keys = new HashSet<string>(StringComparer.Ordinal);
            if (Peek() == '}') { index++; return; }
            while (true) {
                SkipWhitespace();
                if (Peek() != '"') throw new FormatException("object key is not a string at " + index);
                string key = ParseString();
                if (!keys.Add(key)) throw new FormatException("duplicate object key at " + index);
                SkipWhitespace();
                Expect(':');
                ParseValue(depth + 1);
                SkipWhitespace();
                char separator = Peek();
                if (separator == '}') { index++; return; }
                if (separator != ',') throw new FormatException("expected object separator at " + index);
                index++;
            }
        }

        private void ParseArray(int depth) {
            EnterContainer(depth);
            Expect('[');
            SkipWhitespace();
            if (Peek() == ']') { index++; return; }
            while (true) {
                ParseValue(depth + 1);
                SkipWhitespace();
                char separator = Peek();
                if (separator == ']') { index++; return; }
                if (separator != ',') throw new FormatException("expected array separator at " + index);
                index++;
            }
        }

        private string ParseString() {
            Expect('"');
            StringBuilder builder = new StringBuilder();
            while (true) {
                if (index >= text.Length) throw new FormatException("unterminated JSON string");
                char c = text[index++];
                if (c == '"') return builder.ToString();
                if (c < 0x20) throw new FormatException("control character in JSON string");
                if (c != '\\') {
                    builder.Append(c);
                    if (builder.Length > MaximumStringCharacters)
                        throw new FormatException("maximum JSON string length exceeded");
                    continue;
                }
                if (index >= text.Length) throw new FormatException("unterminated JSON escape");
                char escape = text[index++];
                if (escape == '"' || escape == '\\' || escape == '/') builder.Append(escape);
                else if (escape == 'b') builder.Append('\b');
                else if (escape == 'f') builder.Append('\f');
                else if (escape == 'n') builder.Append('\n');
                else if (escape == 'r') builder.Append('\r');
                else if (escape == 't') builder.Append('\t');
                else if (escape == 'u') {
                    if (index + 4 > text.Length) throw new FormatException("short Unicode escape");
                    int code;
                    if (!Int32.TryParse(
                        text.Substring(index, 4),
                        System.Globalization.NumberStyles.AllowHexSpecifier,
                        System.Globalization.CultureInfo.InvariantCulture,
                        out code)) throw new FormatException("invalid Unicode escape");
                    builder.Append((char)code);
                    index += 4;
                } else throw new FormatException("invalid JSON escape at " + (index - 1));
                if (builder.Length > MaximumStringCharacters)
                    throw new FormatException("maximum JSON string length exceeded");
            }
        }

        private void ParseLiteral(string literal) {
            if (index + literal.Length > text.Length ||
                String.CompareOrdinal(text, index, literal, 0, literal.Length) != 0)
                throw new FormatException("invalid JSON literal at " + index);
            index += literal.Length;
        }

        private void ParseNumber() {
            if (Peek() == '-') index++;
            if (index >= text.Length) throw new FormatException("truncated JSON number");
            if (text[index] == '0') index++;
            else {
                if (text[index] < '1' || text[index] > '9') throw new FormatException("invalid JSON number");
                while (index < text.Length && text[index] >= '0' && text[index] <= '9') index++;
            }
            if (index < text.Length && text[index] == '.') {
                index++;
                int start = index;
                while (index < text.Length && text[index] >= '0' && text[index] <= '9') index++;
                if (index == start) throw new FormatException("invalid JSON fraction");
            }
            if (index < text.Length && (text[index] == 'e' || text[index] == 'E')) {
                index++;
                if (index < text.Length && (text[index] == '+' || text[index] == '-')) index++;
                int start = index;
                while (index < text.Length && text[index] >= '0' && text[index] <= '9') index++;
                if (index == start) throw new FormatException("invalid JSON exponent");
            }
        }
    }
}
'@
}

function Resolve-NormalizedPath([string]$Path, [string]$Base) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "path is empty"
    }
    $candidate = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $Base $Path }
    return [IO.Path]::GetFullPath($candidate)
}

function Resolve-PythonInterpreterPath([string]$ExecutablePath) {
    $fullPath = [IO.Path]::GetFullPath($ExecutablePath)
    if ([IO.Path]::GetFileName($fullPath) -ieq "py.exe") {
        throw "py.exe launchers are not accepted; provide the direct python.exe interpreter path"
    }
    return $fullPath
}

function Resolve-Python([string]$Candidate) {
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return Resolve-PythonInterpreterPath -ExecutablePath ([IO.Path]::GetFullPath($Candidate))
        }
        $requested = @(
            Get-Command -Name $Candidate -CommandType Application -ErrorAction Stop |
                Where-Object { [IO.Path]::GetExtension($_.Source) -ieq ".exe" }
        )
        if ($requested.Count -eq 0) { throw "Python command did not resolve to an executable: $Candidate" }
        return Resolve-PythonInterpreterPath -ExecutablePath $requested[0].Source
    }
    $command = @(
        Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object { [IO.Path]::GetExtension($_.Source) -ieq ".exe" }
    )
    if ($command.Count -eq 0) {
        $command = @(
            Get-Command py -CommandType Application -ErrorAction SilentlyContinue |
                Where-Object { [IO.Path]::GetExtension($_.Source) -ieq ".exe" }
        )
    }
    if ($command.Count -eq 0) {
        throw "no Python executable was found"
    }
    return Resolve-PythonInterpreterPath -ExecutablePath $command[0].Source
}

function Test-PathWithinRoot([string]$Path, [string]$Root) {
    $normalizedPath = [IO.Path]::GetFullPath($Path)
    $normalizedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $prefix = $normalizedRoot + [IO.Path]::DirectorySeparatorChar
    return $normalizedPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseSegments([string]$Path, [bool]$AllowMissingLeaf = $false) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $pathRoot = [IO.Path]::GetPathRoot($fullPath)
    $relative = $fullPath.Substring($pathRoot.Length)
    $segments = @($relative.Split(@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar), [StringSplitOptions]::RemoveEmptyEntries))
    $current = $pathRoot
    for ($index = 0; $index -lt $segments.Count; $index++) {
        $current = Join-Path $current $segments[$index]
        if (-not (Test-Path -LiteralPath $current)) {
            if ($AllowMissingLeaf -and $index -eq $segments.Count - 1) { return }
            throw "path segment is missing: $current"
        }
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "path contains a reparse-point segment: $current"
        }
    }
}

function Assert-LocalTempRootSyntax([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $pathRoot = [IO.Path]::GetPathRoot($fullPath)
    if (
        $fullPath.StartsWith("\\", [StringComparison]::Ordinal) -or
        $pathRoot -notmatch '^[A-Za-z]:\\$' -or
        $fullPath.Substring(2).Contains(":")
    ) {
        throw "TEMP must use a canonical local drive path (no UNC, device, or ADS alias)"
    }
    foreach ($segment in $fullPath.Substring($pathRoot.Length).Split([IO.Path]::DirectorySeparatorChar)) {
        if ($segment.Contains("~")) { throw "TEMP must not use an 8.3 short-name alias" }
    }
    $driveName = $pathRoot.Substring(0, 1)
    $drive = Get-PSDrive -Name $driveName -PSProvider FileSystem -ErrorAction Stop
    if (
        [IO.Path]::GetFullPath([string]$drive.Root) -ine $pathRoot -or
        -not [string]::IsNullOrWhiteSpace([string]$drive.DisplayRoot)
    ) {
        throw "TEMP must not use a mapped or substituted drive"
    }
}

function ConvertTo-LowerCrockfordBase32([byte[]]$Bytes) {
    if ($null -eq $Bytes -or $Bytes.Length -ne 16) {
        throw "Crockford Base32 input must contain exactly 16 bytes"
    }
    $alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    $builder = [Text.StringBuilder]::new(26)
    [long]$buffer = 0L
    [int]$bits = 0
    foreach ($value in $Bytes) {
        $buffer = ($buffer -shl 8) -bor [int]$value
        $bits += 8
        while ($bits -ge 5) {
            $bits -= 5
            $index = [int](($buffer -shr $bits) -band 31L)
            [void]$builder.Append($alphabet[$index])
            if ($bits -eq 0) {
                $buffer = 0L
            } else {
                $buffer = $buffer -band ((1L -shl $bits) - 1L)
            }
        }
    }
    if ($bits -gt 0) {
        $index = [int](($buffer -shl (5 - $bits)) -band 31L)
        [void]$builder.Append($alphabet[$index])
    }
    if ($builder.Length -ne 26) { throw "Crockford Base32 output was not 26 characters" }
    return $builder.ToString()
}

function Get-FreshOwnedExecutionRootCandidate {
    $tempRootItem = Get-Item -LiteralPath ([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) -Force
    if (-not $tempRootItem.PSIsContainer) { throw "TEMP is not a directory" }
    $rawTempRoot = [IO.Path]::GetFullPath($tempRootItem.FullName)
    $volumeRoot = [IO.Path]::GetPathRoot($rawTempRoot)
    $tempRoot = if ($rawTempRoot.Length -eq $volumeRoot.Length) {
        $volumeRoot
    } else {
        $rawTempRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    }
    Assert-NoReparseSegments -Path $tempRoot
    Assert-LocalTempRootSyntax -Path $tempRoot
    $tempRootIdentity = [OniRunner.NativeFileIdentity]::Read($tempRoot, $true)

    $randomBytes = [byte[]]::new(16)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    if ($null -eq $random) { throw "cryptographic random-number generator was unavailable" }
    try {
        $random.GetBytes($randomBytes)
    } finally {
        $random.Dispose()
    }
    $token = ConvertTo-LowerCrockfordBase32 -Bytes $randomBytes
    $name = "r" + $token
    if (-not [Text.RegularExpressions.Regex]::IsMatch(
        $name,
        '^r[0123456789abcdefghjkmnpqrstvwxyz]{25}[048cgmrw]$',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )) {
        throw "execution-root name did not match its strict owned format"
    }

    $candidate = [IO.Path]::GetFullPath((Join-Path $tempRoot $name))
    if (
        [IO.Path]::GetDirectoryName($candidate) -ine $tempRoot -or
        -not (Test-PathWithinRoot -Path $candidate -Root $tempRoot)
    ) {
        throw "execution root escaped the resolved TEMP directory"
    }
    if (Test-Path -LiteralPath $candidate) { throw "execution root was not fresh" }
    return [pscustomobject]@{
        path = $candidate
        tempRoot = $tempRoot
        tempRootIdentity = $tempRootIdentity.Identity
    }
}

function Assert-LocalCanonicalPathSyntax([string]$Path, [string]$Label) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    $projectDriveRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($ProjectRoot))
    if (
        $fullPath.StartsWith("\\", [StringComparison]::Ordinal) -or
        $root -notmatch '^[A-Za-z]:\\$' -or
        $root -ine $projectDriveRoot -or
        $fullPath.Substring(2).Contains(":")
    ) {
        throw "$Label must use the workspace's canonical local drive path (no UNC, device, SUBST, or ADS alias)"
    }
    foreach ($segment in $fullPath.Substring($root.Length).Split([IO.Path]::DirectorySeparatorChar)) {
        if ($segment.Contains("~")) { throw "$Label must not use an 8.3 short-name alias" }
    }
}

function Get-BytesSha256([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($Bytes)
        return ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Read-StreamBytesBounded([IO.FileStream]$Stream, [long]$MaximumBytes, [string]$Label) {
    if (-not $Stream.CanRead -or -not $Stream.CanSeek) { throw "$Label stream is not seekable/readable" }
    [void]$Stream.Seek(0L, [IO.SeekOrigin]::Begin)
    $memory = [IO.MemoryStream]::new()
    $buffer = [byte[]]::new(65536)
    try {
        while ($true) {
            $read = $Stream.Read($buffer, 0, $buffer.Length)
            if ($read -eq 0) { break }
            if ($memory.Length + $read -gt $MaximumBytes) {
                throw "$Label exceeds the $MaximumBytes-byte snapshot limit"
            }
            $memory.Write($buffer, 0, $read)
        }
        return $memory.ToArray()
    } finally {
        $memory.Dispose()
    }
}

function Read-OpenFileSnapshot([string]$Path, [IO.FileStream]$Stream) {
    $itemBefore = Get-Item -LiteralPath $Path -Force
    if ($itemBefore.PSIsContainer) { throw "required file is a directory: $Path" }
    if (($itemBefore.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "refusing reparse-point file: $Path"
    }
    if ($itemBefore.Length -gt $script:MaximumSnapshotBytes) {
        throw "file exceeds the $($script:MaximumSnapshotBytes)-byte snapshot limit: $Path"
    }
    $handleBefore = [OniRunner.NativeFileIdentity]::ReadHandle($Stream.SafeFileHandle)
    $pathBefore = [OniRunner.NativeFileIdentity]::Read($Path, $false)
    if ($handleBefore.Identity -cne $pathBefore.Identity) { throw "open handle does not match source path: $Path" }
    if ($handleBefore.LinkCount -ne 1 -or $pathBefore.LinkCount -ne 1) { throw "refusing multiply-linked file: $Path" }

    $bytes = Read-StreamBytesBounded -Stream $Stream -MaximumBytes $script:MaximumSnapshotBytes -Label $Path
    $itemAfter = Get-Item -LiteralPath $Path -Force
    $handleAfter = [OniRunner.NativeFileIdentity]::ReadHandle($Stream.SafeFileHandle)
    $pathAfter = [OniRunner.NativeFileIdentity]::Read($Path, $false)
    if (
        $bytes.Length -ne $itemAfter.Length -or
        $itemBefore.Length -ne $itemAfter.Length -or
        $itemBefore.LastWriteTimeUtc.Ticks -ne $itemAfter.LastWriteTimeUtc.Ticks -or
        $itemBefore.CreationTimeUtc.Ticks -ne $itemAfter.CreationTimeUtc.Ticks -or
        $itemBefore.Attributes -ne $itemAfter.Attributes -or
        $handleBefore.Identity -cne $handleAfter.Identity -or
        $handleBefore.LinkCount -ne $handleAfter.LinkCount -or
        $handleAfter.Identity -cne $pathAfter.Identity -or
        $pathBefore.Identity -cne $pathAfter.Identity -or
        $pathBefore.LinkCount -ne $pathAfter.LinkCount
    ) {
        throw "file identity changed while reading: $Path"
    }
    return [pscustomobject]@{
        exists = $true
        length = [long]$bytes.Length
        sha256 = Get-BytesSha256 -Bytes $bytes
        lastWriteTimeUtcTicks = [long]$itemAfter.LastWriteTimeUtc.Ticks
        creationTimeUtcTicks = [long]$itemAfter.CreationTimeUtc.Ticks
        attributes = [int]$itemAfter.Attributes
        fileIdentity = $handleAfter.Identity
        linkCount = [long]$handleAfter.LinkCount
        bytes = $bytes
    }
}

function Read-FileSnapshot([string]$Path, [bool]$AllowMissing = $false) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        if ($AllowMissing) {
            return [pscustomobject]@{
                exists = $false
                length = 0L
                sha256 = $null
                lastWriteTimeUtcTicks = $null
                creationTimeUtcTicks = $null
                attributes = $null
                fileIdentity = $null
                linkCount = $null
                bytes = $null
            }
        }
        throw "required file is missing: $Path"
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        return Read-OpenFileSnapshot -Path $Path -Stream $stream
    } finally {
        $stream.Dispose()
    }
}

function Get-SnapshotSummary([object]$Snapshot) {
    if ($null -eq $Snapshot) { return $null }
    return [ordered]@{
        exists = $Snapshot.exists
        length = $Snapshot.length
        sha256 = $Snapshot.sha256
        lastWriteTimeUtcTicks = $Snapshot.lastWriteTimeUtcTicks
        creationTimeUtcTicks = $Snapshot.creationTimeUtcTicks
        attributes = $Snapshot.attributes
        fileIdentity = $Snapshot.fileIdentity
        linkCount = $Snapshot.linkCount
    }
}

function Assert-SameCanonicalState([object]$Expected, [object]$Observed, [string]$Stage) {
    if (
        $Expected.exists -ne $Observed.exists -or
        $Expected.length -ne $Observed.length -or
        $Expected.sha256 -cne $Observed.sha256 -or
        $Expected.lastWriteTimeUtcTicks -ne $Observed.lastWriteTimeUtcTicks -or
        $Expected.creationTimeUtcTicks -ne $Observed.creationTimeUtcTicks -or
        $Expected.attributes -ne $Observed.attributes -or
        $Expected.fileIdentity -cne $Observed.fileIdentity -or
        $Expected.linkCount -ne $Observed.linkCount
    ) {
        throw "canonical report drifted during $Stage"
    }
}

function Test-PublishedStateIsCurrent([string]$CanonicalPath, [object]$PublishedSnapshot) {
    if ($null -eq $PublishedSnapshot) { return $false }
    try {
        $observed = Read-FileSnapshot -Path $CanonicalPath -AllowMissing $true
        Assert-SameCanonicalState -Expected $PublishedSnapshot -Observed $observed -Stage "publication-fact check"
        return $true
    } catch {
        return $false
    }
}

function Get-BoundedText([object]$Value, [int]$MaximumCharacters = $script:MaximumFailureTextChars) {
    if ($null -eq $Value) { return $null }
    $text = if ($Value -is [string]) { [string]$Value } else { "<non-string $($Value.GetType().FullName)>" }
    if ($text.Length -le $MaximumCharacters) { return $text }
    return $text.Substring(0, $MaximumCharacters) + "...[truncated]"
}

function Invoke-PublishRollback(
    [string]$CanonicalPath,
    [object]$PreSnapshot,
    [object]$PublishedSnapshot,
    [string]$BackupPath,
    [bool]$DestinationExisted
) {
    $evidence = [ordered]@{
        attempted = $true
        action = $null
        succeeded = $false
        restored = $false
        error = $null
        backupPath = $BackupPath
        publishedState = Get-SnapshotSummary -Snapshot $PublishedSnapshot
        backupState = $null
        displacedPublishedPath = $null
        displacedPublishedState = $null
        cleanupSucceeded = $null
        cleanupError = $null
        currentBefore = $null
        currentAfter = $null
    }
    try {
        if ($null -eq $PublishedSnapshot -or -not $PublishedSnapshot.exists) {
            throw "published canonical state was not captured"
        }
        $current = Read-FileSnapshot -Path $CanonicalPath -AllowMissing $true
        $evidence.currentBefore = Get-SnapshotSummary -Snapshot $current
        Assert-SameCanonicalState -Expected $PublishedSnapshot -Observed $current -Stage "rollback ownership check"
        if ($DestinationExisted) {
            if ([string]::IsNullOrWhiteSpace($BackupPath) -or -not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
                throw "rollback backup is missing"
            }
            $backupSnapshot = Read-FileSnapshot -Path $BackupPath
            $evidence.backupState = Get-SnapshotSummary -Snapshot $backupSnapshot
            Assert-SameCanonicalState -Expected $PreSnapshot -Observed $backupSnapshot -Stage "rollback backup verification"
            $displacedPath = Join-Path (Split-Path -Parent $CanonicalPath) ("." + [IO.Path]::GetFileName($CanonicalPath) + "." + [guid]::NewGuid().ToString("N") + ".rollback.displaced")
            $evidence.displacedPublishedPath = $displacedPath
            [IO.File]::Replace($BackupPath, $CanonicalPath, $displacedPath, $true)
            $evidence.action = "restore_backup"
        } else {
            Remove-Item -LiteralPath $CanonicalPath -Force
            $evidence.action = "remove_new_canonical"
        }
        $after = Read-FileSnapshot -Path $CanonicalPath -AllowMissing $true
        $evidence.currentAfter = Get-SnapshotSummary -Snapshot $after
        Assert-SameCanonicalState -Expected $PreSnapshot -Observed $after -Stage "rollback recovery verification"
        $evidence.restored = $true
        if ($DestinationExisted) {
            $displacedSnapshot = Read-FileSnapshot -Path $evidence.displacedPublishedPath
            $evidence.displacedPublishedState = Get-SnapshotSummary -Snapshot $displacedSnapshot
            Assert-SameCanonicalState -Expected $PublishedSnapshot -Observed $displacedSnapshot -Stage "rollback displaced-published verification"
            try {
                $displacedBeforeDelete = Read-FileSnapshot -Path $evidence.displacedPublishedPath
                Assert-SameCanonicalState -Expected $displacedSnapshot -Observed $displacedBeforeDelete -Stage "rollback displaced cleanup ownership check"
                Remove-Item -LiteralPath $evidence.displacedPublishedPath -Force
                if (Test-Path -LiteralPath $evidence.displacedPublishedPath) { throw "displaced published file remained after cleanup" }
                $evidence.cleanupSucceeded = $true
            } catch {
                $evidence.cleanupSucceeded = $false
                $evidence.cleanupError = Get-BoundedText -Value $_.Exception.Message
                throw "rollback restored canonical but displaced-file cleanup failed: $($evidence.cleanupError)"
            }
        } else {
            $evidence.cleanupSucceeded = $true
        }
        $evidence.succeeded = $true
    } catch {
        $evidence.error = Get-BoundedText -Value $_.Exception.Message
        try {
            $afterFailure = Read-FileSnapshot -Path $CanonicalPath -AllowMissing $true
            $evidence.currentAfter = Get-SnapshotSummary -Snapshot $afterFailure
        } catch {
            if ([string]::IsNullOrWhiteSpace($evidence.error)) {
                $evidence.error = Get-BoundedText -Value $_.Exception.Message
            } else {
                $evidence.error = Get-BoundedText -Value ($evidence.error + "; post-rollback snapshot failed: " + $_.Exception.Message)
            }
        }
    }
    return $evidence
}

function Get-PublishParentState([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if (-not $item.PSIsContainer) { throw "publish parent is not a directory: $Path" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "publish parent must not be a reparse point: $Path"
    }
    $identity = [OniRunner.NativeFileIdentity]::Read($item.FullName, $true)
    return [pscustomobject]@{
        fullName = [IO.Path]::GetFullPath($item.FullName)
        creationTimeUtcTicks = [long]$item.CreationTimeUtc.Ticks
        attributes = [int]$item.Attributes
        fileIdentity = $identity.Identity
    }
}

function Assert-SamePublishParent([object]$Expected, [object]$Observed) {
    if (
        $Expected.fullName -ine $Observed.fullName -or
        $Expected.creationTimeUtcTicks -ne $Observed.creationTimeUtcTicks -or
        $Expected.attributes -ne $Observed.attributes -or
        $Expected.fileIdentity -cne $Observed.fileIdentity
    ) {
        throw "canonical publish parent identity changed"
    }
}

function Get-SourceHashes([Collections.IDictionary]$SourcePaths) {
    $result = [ordered]@{}
    foreach ($entry in $SourcePaths.GetEnumerator()) {
        $snapshot = Read-FileSnapshot -Path ([string]$entry.Value)
        $result[[string]$entry.Key] = Get-SnapshotSummary -Snapshot $snapshot
    }
    return $result
}

function Assert-SourceHashes([Collections.IDictionary]$SourcePaths, [Collections.IDictionary]$Expected, [string]$Stage) {
    $observed = Get-SourceHashes -SourcePaths $SourcePaths
    foreach ($name in $Expected.Keys) {
        foreach ($property in @(
            "length", "sha256", "lastWriteTimeUtcTicks", "creationTimeUtcTicks",
            "attributes", "fileIdentity", "linkCount"
        )) {
            if ($Expected[$name][$property] -cne $observed[$name][$property]) {
                throw "source file changed during ${Stage}: $name ($property)"
            }
        }
    }
    return $observed
}

function Open-LockedSources([Collections.IDictionary]$SourcePaths) {
    $entries = [Collections.Generic.List[object]]::new()
    $summaries = [ordered]@{}
    try {
        $orderedEntries = @($SourcePaths.GetEnumerator() | Sort-Object { ([string]$_.Value).ToLowerInvariant() })
        foreach ($sourceEntry in $orderedEntries) {
            $name = [string]$sourceEntry.Key
            $path = [string]$sourceEntry.Value
            $stream = [IO.FileStream]::new(
                $path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read,
                65536,
                [IO.FileOptions]::SequentialScan
            )
            $lockedEntry = [pscustomobject]@{ name = $name; path = $path; stream = $stream }
            [void]$entries.Add($lockedEntry)
            $snapshot = Read-OpenFileSnapshot -Path $path -Stream $stream
            $summaries[$name] = Get-SnapshotSummary -Snapshot $snapshot
        }
        return [pscustomobject]@{ entries = $entries; summaries = $summaries }
    } catch {
        for ($index = $entries.Count - 1; $index -ge 0; $index--) {
            try { $entries[$index].stream.Dispose() } catch { }
        }
        throw
    }
}

function Assert-LockedSources([object]$LockedSources, [Collections.IDictionary]$Expected, [string]$Stage) {
    if ($null -eq $LockedSources) { throw "source handles were not acquired" }
    $observed = [ordered]@{}
    foreach ($entry in $LockedSources.entries) {
        Assert-NoReparseSegments -Path $entry.path
        $snapshot = Read-OpenFileSnapshot -Path $entry.path -Stream $entry.stream
        $summary = Get-SnapshotSummary -Snapshot $snapshot
        $name = [string]$entry.name
        foreach ($property in @(
            "length", "sha256", "lastWriteTimeUtcTicks", "creationTimeUtcTicks",
            "attributes", "fileIdentity", "linkCount"
        )) {
            if ($Expected[$name][$property] -cne $summary[$property]) {
                throw "source file changed during ${Stage}: $name ($property)"
            }
        }
        $observed[$name] = $summary
    }
    return $observed
}

function Close-LockedSources([object]$LockedSources) {
    if ($null -eq $LockedSources) { return }
    $entries = @($LockedSources.entries)
    $errors = [Collections.Generic.List[string]]::new()
    for ($index = $entries.Count - 1; $index -ge 0; $index--) {
        try { $entries[$index].stream.Dispose() } catch {
            [void]$errors.Add("$($entries[$index].name): $($_.Exception.Message)")
        }
    }
    if ($errors.Count -gt 0) { throw ([string]::Join("; ", $errors)) }
}

function Assert-LockedSourcesDenyWrite([object]$LockedSources) {
    if ($null -eq $LockedSources) { throw "source handles were not held through the publication transaction" }
    foreach ($entry in $LockedSources.entries) {
        if (@(".py", ".ps1") -inotcontains [IO.Path]::GetExtension([string]$entry.path)) { continue }
        $probe = $null
        try {
            $probe = [IO.FileStream]::new(
                [string]$entry.path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Write,
                [IO.FileShare]::Read
            )
        } catch {
            $probeException = $_.Exception
            while ($null -ne $probeException.InnerException) { $probeException = $probeException.InnerException }
            $nativeCode = $probeException.HResult -band 0xffff
            if ($nativeCode -ne 32) {
                throw "source-handle transaction probe failed for $($entry.name): $($probeException.Message)"
            }
            continue
        } finally {
            if ($null -ne $probe) { $probe.Dispose() }
        }
        throw "source handle did not deny write access after the publication transaction: $($entry.name)"
    }
}

function ConvertFrom-StrictUtf8JsonBytes([byte[]]$Bytes, [string]$Label) {
    if ($Bytes.Length -eq 0) { throw "$Label is empty" }
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        throw "$Label must be UTF-8 without a BOM"
    }
    $utf8 = [Text.UTF8Encoding]::new($false, $true)
    try {
        $text = $utf8.GetString($Bytes)
    } catch {
        throw "$Label is not strict UTF-8"
    }
    try {
        [OniRunner.StrictJsonScanner]::ValidateObject($text)
    } catch {
        throw "$Label is not strict JSON or contains duplicate keys: $($_.Exception.Message)"
    }
    try {
        $document = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "$Label is not valid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -is [Array] -or $document -is [string]) {
        throw "$Label root must be a JSON object"
    }
    return $document
}

function Get-JsonPropertyNames([object]$Value) {
    if ($null -eq $Value) { return @() }
    return @($Value.PSObject.Properties | ForEach-Object { $_.Name })
}

function Assert-ExactPropertySet([object]$Value, [string[]]$Expected, [string]$Label) {
    $actual = @(Get-JsonPropertyNames -Value $Value)
    if ($actual.Count -ne $Expected.Count) {
        throw "$Label property count is $($actual.Count), expected $($Expected.Count)"
    }
    foreach ($name in $Expected) {
        if ($actual -cnotcontains $name) { throw "$Label is missing property: $name" }
    }
    foreach ($name in $actual) {
        if ($Expected -cnotcontains $name) { throw "$Label contains unexpected property: $name" }
    }
}

function Assert-ExactString([object]$Value, [string]$Expected, [string]$Label) {
    if ($Value -isnot [string] -or $Value -cne $Expected) {
        throw "$Label must be the exact string '$Expected'"
    }
}

function Assert-NonemptyString([object]$Value, [string]$Label) {
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label must be a non-empty string"
    }
}

function Assert-ExactInteger([object]$Value, [long]$Expected, [string]$Label) {
    $isInteger = $Value -is [sbyte] -or $Value -is [byte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]
    if (-not $isInteger -or [long]$Value -ne $Expected) {
        throw "$Label must be the integer $Expected"
    }
}

function Assert-NonnegativeFiniteNumber([object]$Value, [string]$Label) {
    if ($Value -is [bool] -or $Value -is [string] -or $null -eq $Value) {
        throw "$Label must be a finite number"
    }
    try { $number = [double]$Value } catch { throw "$Label must be a finite number" }
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number) -or $number -lt 0) {
        throw "$Label must be a non-negative finite number"
    }
}

function Assert-ExactFalse([object]$Value, [string]$Label) {
    if ($Value -isnot [bool] -or $Value -ne $false) { throw "$Label must be boolean false" }
}

function Assert-ExactTrue([object]$Value, [string]$Label) {
    if ($Value -isnot [bool] -or $Value -ne $true) { throw "$Label must be boolean true" }
}

function Assert-PycacheAttestation(
    [object]$Document,
    [string]$ExpectedPrefix,
    [string[]]$ExpectedSources,
    [string[]]$RequiredLoadedSources,
    [string[]]$ModuleRoots
) {
    $topKeys = @(
        "schema", "status", "pycachePrefix", "isolated", "dontWriteBytecode",
        "childEnvironmentSanitized",
        "prefixEmptyBeforeImport", "prefixEmptyAfterMain", "inheritedPycachePrefix",
        "attestedSources", "loadedProjectModules", "projectNamespaces"
    )
    Assert-ExactPropertySet -Value $Document -Expected $topKeys -Label "pycache attestation"
    Assert-ExactString -Value $Document.schema -Expected "oni.live-runtime-bootstrap-pycache-attestation.v1" -Label "pycache attestation.schema"
    Assert-ExactString -Value $Document.status -Expected "passed" -Label "pycache attestation.status"
    Assert-ExactTrue -Value $Document.isolated -Label "pycache attestation.isolated"
    Assert-ExactTrue -Value $Document.dontWriteBytecode -Label "pycache attestation.dontWriteBytecode"
    Assert-ExactTrue -Value $Document.childEnvironmentSanitized -Label "pycache attestation.childEnvironmentSanitized"
    Assert-ExactTrue -Value $Document.prefixEmptyBeforeImport -Label "pycache attestation.prefixEmptyBeforeImport"
    Assert-ExactTrue -Value $Document.prefixEmptyAfterMain -Label "pycache attestation.prefixEmptyAfterMain"
    foreach ($value in @($Document.pycachePrefix, $Document.inheritedPycachePrefix)) {
        Assert-NonemptyString -Value $value -Label "pycache attestation prefix"
        if ([IO.Path]::GetFullPath([string]$value) -ine $ExpectedPrefix) {
            throw "pycache attestation prefix does not match the run-scoped prefix"
        }
    }

    $expectedSet = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($source in $ExpectedSources) { [void]$expectedSet.Add([IO.Path]::GetFullPath($source)) }
    $attestedSet = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($source in @($Document.attestedSources)) {
        Assert-NonemptyString -Value $source -Label "pycache attestation.attestedSources"
        $fullSource = [IO.Path]::GetFullPath([string]$source)
        if (-not $attestedSet.Add($fullSource)) { throw "pycache attestation contains duplicate attested source paths" }
    }
    if (-not $expectedSet.SetEquals($attestedSet)) { throw "pycache attestation held-source set mismatch" }

    $loadedSources = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $moduleNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $modules = @($Document.loadedProjectModules)
    if ($modules.Count -eq 0) { throw "pycache attestation has no loaded project modules" }
    foreach ($module in $modules) {
        Assert-ExactPropertySet -Value $module -Expected @("name", "filePath", "specOrigin", "cachedPath", "specCachedPath") -Label "pycache attestation loaded module"
        Assert-NonemptyString -Value $module.name -Label "pycache attestation module.name"
        Assert-NonemptyString -Value $module.filePath -Label "pycache attestation module.filePath"
        if (-not $moduleNames.Add([string]$module.name)) { throw "pycache attestation contains duplicate module names" }
        $moduleFile = [IO.Path]::GetFullPath([string]$module.filePath)
        if (-not $expectedSet.Contains($moduleFile)) { throw "loaded project module is outside the held source set: $($module.name)" }
        [void]$loadedSources.Add($moduleFile)
        if ($null -ne $module.specOrigin -and [IO.Path]::GetFullPath([string]$module.specOrigin) -ine $moduleFile) {
            throw "loaded project module specOrigin mismatch: $($module.name)"
        }
        foreach ($cached in @($module.cachedPath, $module.specCachedPath)) {
            if ($null -eq $cached) {
                if ($module.name -cne "__main__") { throw "loaded project module is missing cache metadata: $($module.name)" }
                continue
            }
            Assert-NonemptyString -Value $cached -Label "pycache attestation module cache path"
            $cachePath = [IO.Path]::GetFullPath([string]$cached)
            if (-not (Test-PathWithinRoot -Path $cachePath -Root $ExpectedPrefix)) {
                throw "loaded project module cache path escaped the run-scoped prefix: $($module.name)"
            }
            if ($cachePath -match '(?i)[\\/]__pycache__[\\/]') {
                throw "loaded project module references a workspace __pycache__: $($module.name)"
            }
            if (Test-Path -LiteralPath $cachePath) { throw "project bytecode cache unexpectedly exists: $cachePath" }
        }
    }
    foreach ($requiredSource in $RequiredLoadedSources) {
        if (-not $loadedSources.Contains([IO.Path]::GetFullPath($requiredSource))) {
            throw "required held project source was not loaded: $requiredSource"
        }
    }

    $namespaceNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($namespace in @($Document.projectNamespaces)) {
        Assert-ExactPropertySet -Value $namespace -Expected @("name", "searchLocations") -Label "pycache attestation namespace"
        Assert-NonemptyString -Value $namespace.name -Label "pycache attestation namespace.name"
        if (-not $namespaceNames.Add([string]$namespace.name)) { throw "pycache attestation contains duplicate namespace names" }
        $locations = @($namespace.searchLocations)
        if ($locations.Count -eq 0) { throw "project namespace has no search locations" }
        foreach ($location in $locations) {
            $insideModuleRoot = $false
            foreach ($moduleRoot in $ModuleRoots) {
                if ([IO.Path]::GetFullPath([string]$location) -ieq [IO.Path]::GetFullPath($moduleRoot) -or (Test-PathWithinRoot -Path ([string]$location) -Root $moduleRoot)) {
                    $insideModuleRoot = $true
                    break
                }
            }
            if (-not $insideModuleRoot) { throw "project namespace search location escaped module roots" }
        }
    }
    return [ordered]@{
        pycachePrefix = $ExpectedPrefix
        isolated = $true
        dontWriteBytecode = $true
        childEnvironmentSanitized = $true
        prefixEmptyBeforeImport = $true
        prefixEmptyAfterMain = $true
        attestedSourceCount = $attestedSet.Count
        loadedProjectModuleCount = $modules.Count
        loadedProjectModules = @($modules | ForEach-Object { $_.name })
        projectNamespaceCount = @($Document.projectNamespaces).Count
    }
}

function Assert-VerificationReport([object]$Report) {
    $topKeys = @(
        "schema", "status", "classification", "round_count", "required_round_count",
        "scenario_count", "assertion_count", "duration_seconds", "static_scenarios",
        "rounds", "failure", "preserved_test_root", "game_launched", "real_klei_writes"
    )
    Assert-ExactPropertySet -Value $Report -Expected $topKeys -Label "report"
    Assert-ExactString -Value $Report.schema -Expected "oni.live-runtime-bootstrap-verification.v1" -Label "report.schema"
    Assert-ExactString -Value $Report.status -Expected "passed" -Label "report.status"
    Assert-ExactString -Value $Report.classification -Expected "static_probe_and_testroot_fault_injection" -Label "report.classification"
    Assert-ExactInteger -Value $Report.round_count -Expected 2 -Label "report.round_count"
    Assert-ExactInteger -Value $Report.required_round_count -Expected 2 -Label "report.required_round_count"
    Assert-ExactInteger -Value $Report.scenario_count -Expected 504 -Label "report.scenario_count"
    Assert-ExactInteger -Value $Report.assertion_count -Expected 4032 -Label "report.assertion_count"
    Assert-NonnegativeFiniteNumber -Value $Report.duration_seconds -Label "report.duration_seconds"
    if ($null -ne $Report.failure) { throw "report.failure must be null" }
    if ($null -ne $Report.preserved_test_root) { throw "report.preserved_test_root must be null" }
    Assert-ExactFalse -Value $Report.game_launched -Label "report.game_launched"
    Assert-ExactFalse -Value $Report.real_klei_writes -Label "report.real_klei_writes"

    $expectedStaticNames = @(
        "manager_has_no_build_and_workflow_has_no_game_launch_surface",
        "process_census_no_self_match",
        "process_get_process_failure_closed",
        "process_query_no_source_text_self_match",
        "process_query_exact_bridge_argv_counted",
        "process_query_bool_pid_refused",
        "legacy_path_0_and_1_unit_boundaries",
        "verifier_injected_marker_legacy_path_regression",
        "legacy_path_budget_enumerates_all_transaction_transients",
        "legacy_path_259_pass_260_fail"
    )
    $staticScenarios = @($Report.static_scenarios)
    if ($staticScenarios.Count -ne 10) { throw "report.static_scenarios must contain exactly 10 entries" }
    for ($index = 0; $index -lt $expectedStaticNames.Count; $index++) {
        $scenario = $staticScenarios[$index]
        Assert-ExactPropertySet -Value $scenario -Expected @("name", "status") -Label "static_scenarios[$index]"
        Assert-ExactString -Value $scenario.name -Expected $expectedStaticNames[$index] -Label "static_scenarios[$index].name"
        Assert-ExactString -Value $scenario.status -Expected "passed" -Label "static_scenarios[$index].status"
    }

    $roundsPayload = @($Report.rounds)
    if ($roundsPayload.Count -ne 2) { throw "report.rounds must contain exactly 2 entries" }
    $summedScenarios = $staticScenarios.Count
    for ($roundIndex = 0; $roundIndex -lt 2; $roundIndex++) {
        $round = $roundsPayload[$roundIndex]
        $roundNumber = $roundIndex + 1
        Assert-ExactPropertySet -Value $round -Expected @("round", "status", "scenario_count", "scenarios") -Label "rounds[$roundIndex]"
        Assert-ExactInteger -Value $round.round -Expected $roundNumber -Label "rounds[$roundIndex].round"
        Assert-ExactString -Value $round.status -Expected "passed" -Label "rounds[$roundIndex].status"
        Assert-ExactInteger -Value $round.scenario_count -Expected 247 -Label "rounds[$roundIndex].scenario_count"
        $scenarios = @($round.scenarios)
        if ($scenarios.Count -ne 247) { throw "rounds[$roundIndex].scenarios must contain exactly 247 entries" }
        $seenNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        $orderedNames = [Collections.Generic.List[string]]::new()
        for ($scenarioIndex = 0; $scenarioIndex -lt $scenarios.Count; $scenarioIndex++) {
            $scenario = $scenarios[$scenarioIndex]
            $properties = @(Get-JsonPropertyNames -Value $scenario)
            $expectedShape = if ($properties -ccontains "classification") {
                @("name", "status", "classification")
            } else {
                @("name", "status")
            }
            Assert-ExactPropertySet -Value $scenario -Expected $expectedShape -Label "rounds[$roundIndex].scenarios[$scenarioIndex]"
            Assert-NonemptyString -Value $scenario.name -Label "rounds[$roundIndex].scenarios[$scenarioIndex].name"
            Assert-ExactString -Value $scenario.status -Expected "passed" -Label "rounds[$roundIndex].scenarios[$scenarioIndex].status"
            if ($properties -ccontains "classification") {
                Assert-NonemptyString -Value $scenario.classification -Label "rounds[$roundIndex].scenarios[$scenarioIndex].classification"
            }
            if (-not $seenNames.Add([string]$scenario.name)) {
                throw "rounds[$roundIndex] contains duplicate scenario name: $($scenario.name)"
            }
            [void]$orderedNames.Add([string]$scenario.name)
        }
        $orderedNameBytes = [Text.UTF8Encoding]::new($false).GetBytes([string]::Join("`n", $orderedNames))
        $orderedNameHash = Get-BytesSha256 -Bytes $orderedNameBytes
        if ($orderedNameHash -cne $script:ExpectedRoundScenarioNamesSha256) {
            throw "rounds[$roundIndex] scenario-name manifest hash mismatch"
        }
        $summedScenarios += $scenarios.Count
    }
    if ($summedScenarios -ne 504) { throw "report scenario total does not equal 504" }
    if ([long]$Report.assertion_count -ne ([long]$Report.scenario_count * 8)) {
        throw "report assertion_count is not scenario_count * 8"
    }
    return [ordered]@{
        schema = $Report.schema
        status = $Report.status
        staticScenarioCount = $staticScenarios.Count
        roundCount = $roundsPayload.Count
        roundScenarioCounts = @($roundsPayload | ForEach-Object { $_.scenario_count })
        roundScenarioNamesSha256 = $script:ExpectedRoundScenarioNamesSha256
        scenarioCount = $Report.scenario_count
        assertionCount = $Report.assertion_count
    }
}

function Test-ByteArrayEqual([byte[]]$Left, [byte[]]$Right) {
    if ($null -eq $Left -or $null -eq $Right -or $Left.Length -ne $Right.Length) { return $false }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Write-BytesCreateNew([string]$Path, [byte[]]$Bytes) {
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function ConvertTo-NativeArgument([string]$Argument) {
    if ($Argument.Length -eq 0) { return '""' }
    if ($Argument -notmatch '[\s"]') { return $Argument }
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ([int]$character -eq 92) {
            $slashes++
        } elseif ($character -eq '"') {
            [void]$builder.Append([char]92, (2 * $slashes) + 1)
            [void]$builder.Append('"')
            $slashes = 0
        } else {
            if ($slashes -gt 0) { [void]$builder.Append([char]92, $slashes) }
            [void]$builder.Append($character)
            $slashes = 0
        }
    }
    if ($slashes -gt 0) { [void]$builder.Append([char]92, 2 * $slashes) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-LogLength([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 0L }
    return [long](Get-Item -LiteralPath $Path).Length
}

function Test-IsMissingFileSystemEntryException([Exception]$Exception) {
    $current = $Exception
    while ($null -ne $current) {
        if (
            $current.GetType() -eq [IO.DirectoryNotFoundException] -or
            $current.GetType() -eq [IO.FileNotFoundException]
        ) {
            return $null -eq $current.InnerException
        }
        if ($current -isnot [Management.Automation.MethodInvocationException]) {
            return $false
        }
        $current = $current.InnerException
    }
    return $false
}

function Assert-ExecutionRootIdentity([string]$Root, [string]$ExpectedIdentity) {
    if ([string]::IsNullOrWhiteSpace($Root) -or [string]::IsNullOrWhiteSpace($ExpectedIdentity)) {
        throw "execution root identity inputs were missing"
    }
    $fullRoot = [IO.Path]::GetFullPath($Root)
    $attributesBefore = [IO.File]::GetAttributes($fullRoot)
    if (($attributesBefore -band [IO.FileAttributes]::Directory) -eq 0) {
        throw "execution root is no longer a directory: $fullRoot"
    }
    if (($attributesBefore -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "execution root became a reparse point: $fullRoot"
    }
    $observedIdentity = ([OniRunner.NativeFileIdentity]::Read($fullRoot, $true)).Identity
    $attributesAfter = [IO.File]::GetAttributes($fullRoot)
    if (
        ($attributesAfter -band [IO.FileAttributes]::Directory) -eq 0 -or
        ($attributesAfter -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $observedIdentity -cne $ExpectedIdentity
    ) {
        throw "execution root identity changed during resource monitoring: $fullRoot"
    }
}

function Test-ExecutionRootEntryDisappeared(
    [string]$Path,
    [string]$Root,
    [string]$ExpectedRootIdentity
) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root)
    Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
    if ($fullPath -ieq $fullRoot) { return $false }
    try {
        [void][IO.File]::GetAttributes($fullPath)
        return $false
    } catch {
        if (-not (Test-IsMissingFileSystemEntryException -Exception $_.Exception)) { throw }
    }
    Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
    return $true
}

function Get-ExecutionRootUsage([string]$Root, [string]$ExpectedRootIdentity) {
    $fullRoot = [IO.Path]::GetFullPath($Root)
    $scanRestarts = 0L
    $transientDisappearances = 0L
    for ($scanAttempt = 1; $scanAttempt -le $script:MaximumExecutionRootScanAttempts; $scanAttempt++) {
        if ($scanAttempt -gt 1) {
            $retryDelayMilliseconds = [int][Math]::Min(25, [Math]::Pow(2, $scanAttempt - 2) * $script:MinimumExecutionRootScanRetryMilliseconds)
            Start-Sleep -Milliseconds $retryDelayMilliseconds
        }
        Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
        $pending = [Collections.Generic.Queue[string]]::new()
        $pending.Enqueue($fullRoot)
        $bytes = 0L
        $files = 0L
        $directories = 0L
        $entries = 0L
        $restartRequired = $false
        while ($pending.Count -gt 0) {
            $directory = $pending.Dequeue()
            try {
                foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
                    try {
                        $attributes = [IO.File]::GetAttributes($entryPath)
                        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                            throw "execution root contains a reparse point: $entryPath"
                        }
                        if (
                            $TestInjectResourceMonitorEnumerationIoFailure -and
                            -not $script:TestResourceMonitorEnumerationIoFailureInjected -and
                            [IO.Path]::GetFileName($entryPath) -ceq "fixture-resource-monitor-io-failure"
                        ) {
                            $script:TestResourceMonitorEnumerationIoFailureInjected = $true
                            throw [IO.IOException]::new("fixture injected resource-monitor enumeration IO failure")
                        }
                        $entryName = [IO.Path]::GetFileName($entryPath)
                        if (
                            ($TestInjectResourceMonitorTransientChurn -or $TestInjectResourceMonitorPersistentChurn) -and
                            $entryName -like "fixture-resource-monitor-churn-*"
                        ) {
                            $injectTransient = (
                                $TestInjectResourceMonitorTransientChurn -and
                                -not $script:TestResourceMonitorTransientChurnInjected
                            )
                            if ($injectTransient -or $TestInjectResourceMonitorPersistentChurn) {
                                [IO.File]::Delete($entryPath)
                                if ([IO.File]::Exists($entryPath)) {
                                    throw "fixture resource-monitor churn injection did not delete its entry"
                                }
                                if ($injectTransient) {
                                    $script:TestResourceMonitorTransientChurnInjected = $true
                                }
                                if ($TestInjectResourceMonitorPersistentChurn) {
                                    $script:TestResourceMonitorPersistentChurnInjectionCount++
                                }
                                [void][IO.File]::GetAttributes($entryPath)
                            }
                        }
                        $isDirectory = (($attributes -band [IO.FileAttributes]::Directory) -ne 0)
                        $entries++
                        if ($isDirectory) {
                            $directories++
                        } else {
                            $files++
                        }
                        if ($entries -gt $script:MaximumExecutionRootEntries) {
                            Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
                            return [pscustomobject]@{
                                bytes = $bytes
                                files = $files
                                directories = $directories
                                entries = $entries
                                scanRestarts = $scanRestarts
                                transientDisappearances = $transientDisappearances
                                limitExceeded = $true
                                unstable = $false
                            }
                        }
                        if ($isDirectory) {
                            $pending.Enqueue($entryPath)
                            if (
                                $TestInjectResourceMonitorNestedRename -and
                                -not $script:TestResourceMonitorNestedRenameInjected -and
                                [IO.Path]::GetFileName($entryPath) -ceq "archive-root" -and
                                $entryPath.StartsWith(
                                    (Join-Path $fullRoot "fixture-resource-monitor-rename-source"),
                                    [StringComparison]::OrdinalIgnoreCase
                                )
                            ) {
                                $renameDestination = Join-Path $fullRoot "fixture-resource-monitor-archive-destination"
                                [IO.Directory]::Move($entryPath, $renameDestination)
                                if (
                                    [IO.Directory]::Exists($entryPath) -or
                                    -not [IO.Directory]::Exists($renameDestination)
                                ) {
                                    throw "fixture nested directory rename did not move the queued path"
                                }
                                $script:TestResourceMonitorNestedRenameInjected = $true
                            }
                            continue
                        }
                        $bytes += [long]([IO.FileInfo]::new($entryPath).Length)
                        if ($bytes -gt $script:MaximumExecutionRootBytes) {
                            Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
                            return [pscustomobject]@{
                                bytes = $bytes
                                files = $files
                                directories = $directories
                                entries = $entries
                                scanRestarts = $scanRestarts
                                transientDisappearances = $transientDisappearances
                                limitExceeded = $true
                                unstable = $false
                            }
                        }
                    } catch {
                        if (
                            -not (Test-IsMissingFileSystemEntryException -Exception $_.Exception) -or
                            -not (Test-ExecutionRootEntryDisappeared -Path $entryPath -Root $fullRoot -ExpectedRootIdentity $ExpectedRootIdentity)
                        ) {
                            throw
                        }
                        $transientDisappearances++
                        $restartRequired = $true
                        break
                    }
                }
            } catch {
                if (
                    -not (Test-IsMissingFileSystemEntryException -Exception $_.Exception) -or
                    -not (Test-ExecutionRootEntryDisappeared -Path $directory -Root $fullRoot -ExpectedRootIdentity $ExpectedRootIdentity)
                ) {
                    throw
                }
                $transientDisappearances++
                $restartRequired = $true
            }
            if ($restartRequired) { break }
        }
        Assert-ExecutionRootIdentity -Root $fullRoot -ExpectedIdentity $ExpectedRootIdentity
        if (-not $restartRequired) {
            return [pscustomobject]@{
                bytes = $bytes
                files = $files
                directories = $directories
                entries = $entries
                scanRestarts = $scanRestarts
                transientDisappearances = $transientDisappearances
                limitExceeded = $false
                unstable = $false
            }
        }
        $scanRestarts++
    }
    return [pscustomobject]@{
        bytes = $bytes
        files = $files
        directories = $directories
        entries = $entries
        scanRestarts = $scanRestarts
        transientDisappearances = $transientDisappearances
        limitExceeded = $false
        unstable = $true
    }
}

function Get-WorkerResourceState(
    [string]$ExecutionRoot,
    [string]$ExpectedExecutionRootIdentity,
    [string]$StdoutLog,
    [string]$StderrLog,
    [string]$WorkerReport,
    [Diagnostics.Stopwatch]$Watch,
    [int]$DeadlineSeconds
) {
    $stdoutBytes = Get-LogLength -Path $StdoutLog
    $stderrBytes = Get-LogLength -Path $StderrLog
    $reportBytes = Get-LogLength -Path $WorkerReport
    $rootUsage = Get-ExecutionRootUsage -Root $ExecutionRoot -ExpectedRootIdentity $ExpectedExecutionRootIdentity
    $violation = $null
    if ($Watch.Elapsed.TotalSeconds -gt $DeadlineSeconds) {
        $violation = "worker exceeded the $DeadlineSeconds-second total deadline"
    } elseif ($stdoutBytes -gt $script:MaximumStdoutBytes) {
        $violation = "worker stdout exceeded the $($script:MaximumStdoutBytes)-byte limit"
    } elseif ($stderrBytes -gt $script:MaximumStderrBytes) {
        $violation = "worker stderr exceeded the $($script:MaximumStderrBytes)-byte limit"
    } elseif ($reportBytes -gt $script:MaximumSnapshotBytes) {
        $violation = "worker report exceeded the observed $($script:MaximumSnapshotBytes)-byte kill threshold"
    } elseif ($rootUsage.limitExceeded) {
        $violation = "worker execution root exceeded its observed byte/entry kill threshold"
    } elseif ($rootUsage.unstable) {
        $violation = "execution root remained unstable after $($script:MaximumExecutionRootScanAttempts) resource-monitor scan attempts"
    }
    return [pscustomobject]@{
        stdoutBytes = [long]$stdoutBytes
        stderrBytes = [long]$stderrBytes
        reportBytes = [long]$reportBytes
        executionRootBytes = [long]$rootUsage.bytes
        executionRootFiles = [long]$rootUsage.files
        executionRootDirectories = [long]$rootUsage.directories
        executionRootEntries = [long]$rootUsage.entries
        executionRootScanRestarts = [long]$rootUsage.scanRestarts
        executionRootTransientDisappearances = [long]$rootUsage.transientDisappearances
        violation = $violation
    }
}

function Limit-LogFile([string]$Path, [long]$MaximumBytes) {
    $length = Get-LogLength -Path $Path
    $truncated = $false
    if ($length -gt $MaximumBytes) {
        $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try {
            $stream.SetLength($MaximumBytes)
            $stream.Flush($true)
            $truncated = $true
        } finally {
            $stream.Dispose()
        }
    }
    return [pscustomobject]@{ observedBytes = [long]$length; truncated = $truncated }
}

function Get-CaptureFailure([object]$Task, [object]$BoundedStream, [string]$Label, [long]$MaximumBytes) {
    if ($null -ne $BoundedStream -and $BoundedStream.LimitExceeded) {
        return "$Label exceeded the $MaximumBytes-byte hard capture limit"
    }
    if ($null -ne $Task -and $Task.IsCanceled) { return "$Label capture task was canceled" }
    if ($null -ne $Task -and $Task.IsFaulted) {
        return "$Label capture failed: $($Task.Exception.GetBaseException().Message)"
    }
    return $null
}

function Send-Heartbeat(
    [string]$Stage,
    [Nullable[int]]$ProcessId,
    [double]$Elapsed,
    [int]$Count,
    [string]$WorkerReport,
    [string]$StdoutLog,
    [string]$StderrLog,
    [Nullable[int]]$WorkerExitCode
) {
    $heartbeat = [ordered]@{
        schema = "oni.live-runtime-bootstrap-verification-heartbeat.v1"
        kind = "heartbeat"
        stage = $Stage
        emittedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        processId = $ProcessId
        elapsedSeconds = [Math]::Round($Elapsed, 3)
        heartbeatCount = $Count
        workerExitCode = $WorkerExitCode
        workerReport = $WorkerReport
        stdoutBytes = Get-LogLength -Path $StdoutLog
        stderrBytes = Get-LogLength -Path $StderrLog
    }
    [Console]::Out.WriteLine(($heartbeat | ConvertTo-Json -Depth 6 -Compress))
}

function Stop-WorkerProcessTree([Diagnostics.Process]$Process, [bool]$Started) {
    if (-not $Started -or $null -eq $Process) { return }
    try {
        if ($Process.HasExited) { return }
    } catch {
        return
    }
    try {
        # .NET Core/modern PowerShell provides the process-tree overload.
        $Process.Kill($true)
        return
    } catch {
        # Windows PowerShell 5.1 lacks Kill(bool); taskkill /T is the bounded
        # fallback for the exact child PID created by this runner.
    }
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    if (Test-Path -LiteralPath $taskkill -PathType Leaf) {
        try { & $taskkill /PID ([string]$Process.Id) /T /F 2>$null | Out-Null } catch { }
    }
}

$startUtc = (Get-Date).ToUniversalTime()
$watch = [Diagnostics.Stopwatch]::StartNew()
$heartbeatCount = 0
$executionRoot = $null
$stdoutLog = $null
$stderrLog = $null
$workerReportPath = $null
$workerAttestationPath = $null
$pycachePrefix = $null
$canonicalPath = $null
$workerExitCode = $null
$workerProcessId = $null
$reportPublished = $false
$validatedSummary = $null
$candidateRawSha256 = $null
$tempRawSha256 = $null
$publishedRawSha256 = $null
$preSourceHashes = $null
$postSourceHashes = $null
$preCanonicalSnapshot = $null
$prePublishCanonicalSnapshot = $null
$postCanonicalSnapshot = $null
$message = ""
$succeeded = $false
$testFixtureMode = $false
$failedReportSummary = $null
$resolvedTestRoot = $null
$jobAssigned = $false
$jobActiveBeforeCleanup = $null
$jobActiveAfterCleanup = $null
$jobCleanupSucceeded = $false
$copyDrainCompleted = $false
$cleanupError = $null
$resourceLimitError = $null
$stdoutBytesObserved = 0L
$stderrBytesObserved = 0L
$reportBytesObserved = 0L
$executionRootBytesObserved = 0L
$executionRootFilesObserved = 0L
$executionRootDirectoriesObserved = 0L
$executionRootEntriesObserved = 0L
$executionRootScanRestartsObserved = 0L
$executionRootTransientDisappearancesObserved = 0L
$executionTempRoot = $null
$executionTempRootIdentityBefore = $null
$executionTempRootIdentityAfter = $null
$executionRootIdentity = $null
$stdoutLogTruncated = $false
$stderrLogTruncated = $false
$sourcePaths = $null
$sourceLocks = $null
$primaryFailure = $null
$sourceAuditError = $null
$canonicalAuditError = $null
$sourceAuditObserved = $null
$canonicalAuditObserved = $null
$mainCompleted = $false
$publishPendingCommit = $false
$publishMutationDone = $false
$publishRollbackCompleted = $false
$publishedCanonicalSnapshot = $null
$backupSnapshot = $null
$backupPath = $null
$publishLockStream = $null
$publishLockPath = $null
$publishLockHandleIdentity = $null
$publishLockExpectedSha256 = $null
$publishLockReleased = $false
$publishLockReleaseError = $null
$publishLockCleanupError = $null
$publishedCanonicalBridgeStream = $null
$publishedCanonicalBridgeError = $null
$publishedCanonicalBridgeHeld = $false
$workerPycacheAttestation = $null
$workerPycacheAttestationRawSha256 = $null
$workerPycacheAttestationRawLength = $null
$sourceHandlesHeldThroughTransaction = $false
$sourceHandleReleaseError = $null
$publicationTransactionCompleted = $false
$rollbackEvidence = [ordered]@{
    attempted = $false
    action = $null
    succeeded = $null
    restored = $null
    error = $null
    backupPath = $null
    publishedState = $null
    backupState = $null
    displacedPublishedPath = $null
    displacedPublishedState = $null
    cleanupSucceeded = $null
    cleanupError = $null
    currentBefore = $null
    currentAfter = $null
}

try {
    $canonicalPath = Resolve-NormalizedPath -Path $CanonicalReport -Base $ProjectRoot
    $workerPath = Resolve-NormalizedPath -Path $WorkerScript -Base $ProjectRoot
    $verifierPath = Resolve-NormalizedPath -Path $VerifierScript -Base $ProjectRoot
    $managerPath = Resolve-NormalizedPath -Path $ManagerScript -Base $ProjectRoot
    $runnerPath = [IO.Path]::GetFullPath($PSCommandPath)
    $pythonPath = Resolve-Python -Candidate $PythonExecutable
    foreach ($pathEntry in @(
        @("runner", $runnerPath), @("worker", $workerPath), @("verifier", $verifierPath),
        @("manager", $managerPath), @("Python", $pythonPath), @("canonical", $canonicalPath)
    )) {
        Assert-LocalCanonicalPathSyntax -Path $pathEntry[1] -Label $pathEntry[0]
    }

    $productionWorker = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "run_live_runtime_bootstrap_verification_worker.py"))
    $productionVerifier = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "verify_live_runtime_bootstrap.py"))
    $productionManager = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "manage_live_runtime_bootstrap.py"))
    $productionCanonical = [IO.Path]::GetFullPath(
        [IO.Path]::Combine($ProjectRoot, "reports", "live-runtime-bootstrap-verification.json")
    )
    $trustedPythonPaths = @()
    foreach ($pythonName in @("py", "python")) {
        $trustedCommands = @(
            Get-Command $pythonName -CommandType Application -ErrorAction SilentlyContinue |
                Where-Object { [IO.Path]::GetExtension($_.Source) -ieq ".exe" }
        )
        foreach ($trustedCommand in $trustedCommands) {
            try {
                $trustedPythonPaths += Resolve-Python -Candidate $trustedCommand.Source
            } catch {
                # An unusable launcher is not a trusted interpreter candidate.
            }
        }
    }
    $hasSourceOverride =
        $workerPath -ine $productionWorker -or
        $verifierPath -ine $productionVerifier -or
        $managerPath -ine $productionManager -or
        $trustedPythonPaths -inotcontains $pythonPath -or
        @($AdditionalSourceFile).Count -gt 0 -or
        -not [string]::IsNullOrWhiteSpace($TestFixtureRoundManifest)
    $testFixtureMode = $hasSourceOverride -or $canonicalPath -ine $productionCanonical
    if ($canonicalPath -ieq $productionCanonical -and $testFixtureMode) {
        throw "production canonical requires the exact production worker, verifier, manager, dependencies, and trusted Python"
    }
    if ($testFixtureMode) {
        if ([string]::IsNullOrWhiteSpace($TestRoot)) {
            throw "fixture/override mode requires an explicit TestRoot"
        }
        $resolvedTestRoot = Resolve-NormalizedPath -Path $TestRoot -Base $ProjectRoot
        Assert-LocalCanonicalPathSyntax -Path $resolvedTestRoot -Label "TestRoot"
        if (-not (Test-Path -LiteralPath $resolvedTestRoot -PathType Container)) {
            throw "TestRoot is not an existing directory: $resolvedTestRoot"
        }
        Assert-NoReparseSegments -Path $resolvedTestRoot
        [void](Get-PublishParentState -Path $resolvedTestRoot)
        if (-not (Test-PathWithinRoot -Path $canonicalPath -Root $resolvedTestRoot)) {
            throw "fixture canonical report must be contained by TestRoot"
        }
        foreach ($fixtureSource in @($workerPath, $verifierPath, $managerPath)) {
            if (
                $fixtureSource -ine $productionWorker -and
                $fixtureSource -ine $productionVerifier -and
                $fixtureSource -ine $productionManager -and
                -not (Test-PathWithinRoot -Path $fixtureSource -Root $resolvedTestRoot)
            ) {
                throw "fixture source override is outside TestRoot: $fixtureSource"
            }
        }
    } elseif (-not [string]::IsNullOrWhiteSpace($TestRoot)) {
        throw "TestRoot is only valid in fixture/override mode"
    }
    if ((
        $TestInjectJobCleanupFailure -or $TestInjectPostPublishFailure -or $TestInjectRollbackIdentitySwap -or
        $TestInjectSourceAuditFailure -or $TestInjectCanonicalAuditFailure -or
        $TestInjectPublishLockReleaseFailure -or $TestInjectSourceHandleReleaseFailure -or
        $TestInjectResourceMonitorNestedRename -or $TestInjectResourceMonitorEnumerationIoFailure -or
        $TestInjectResourceMonitorTransientChurn -or $TestInjectResourceMonitorPersistentChurn -or
        $TestProbeSourceHandlesAfterTransaction
    ) -and -not $testFixtureMode) {
        throw "test fault-injection switches are only valid in fixture/override mode"
    }
    if (($TestInjectPostPublishFailure -or $TestInjectRollbackIdentitySwap) -and $WhatIf) {
        throw "post-publish fault injection cannot be combined with WhatIf"
    }

    foreach ($requiredPath in @($runnerPath, $workerPath, $verifierPath, $managerPath, $pythonPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) { throw "required executable/source is missing: $requiredPath" }
        Assert-NoReparseSegments -Path $requiredPath
    }
    Assert-NoReparseSegments -Path $canonicalPath -AllowMissingLeaf $true
    foreach ($sourcePath in @($runnerPath, $workerPath, $verifierPath, $managerPath)) {
        if ($sourcePath -ieq $canonicalPath) { throw "canonical report overlaps an attested source file" }
    }

    $sourcePaths = [ordered]@{
        runner = $runnerPath
        worker = $workerPath
        verifier = $verifierPath
        manager = $managerPath
        pythonExecutable = $pythonPath
    }
    if (-not [string]::IsNullOrWhiteSpace($TestFixtureRoundManifest)) {
        if ($verifierPath -ieq $productionVerifier -or $canonicalPath -ieq $productionCanonical) {
            throw "test-fixture manifest is forbidden with the production verifier or canonical report"
        }
        $fixtureManifestPath = Resolve-NormalizedPath -Path $TestFixtureRoundManifest -Base $ProjectRoot
        Assert-LocalCanonicalPathSyntax -Path $fixtureManifestPath -Label "test-fixture round manifest"
        if (-not (Test-Path -LiteralPath $fixtureManifestPath -PathType Leaf)) {
            throw "test-fixture round manifest is missing: $fixtureManifestPath"
        }
        Assert-NoReparseSegments -Path $fixtureManifestPath
        if (-not (Test-PathWithinRoot -Path $fixtureManifestPath -Root $resolvedTestRoot)) {
            throw "test-fixture round manifest must be contained by TestRoot"
        }
        $manifestSnapshot = Read-FileSnapshot -Path $fixtureManifestPath
        $manifestDocument = ConvertFrom-StrictUtf8JsonBytes -Bytes $manifestSnapshot.bytes -Label "test-fixture round manifest"
        Assert-ExactPropertySet -Value $manifestDocument -Expected @("schema", "names") -Label "test-fixture round manifest"
        Assert-ExactString -Value $manifestDocument.schema -Expected "oni.live-runtime-bootstrap-runner-test-manifest.v1" -Label "test-fixture round manifest.schema"
        $fixtureNames = @($manifestDocument.names)
        if ($fixtureNames.Count -ne 247) { throw "test-fixture round manifest must contain exactly 247 names" }
        $fixtureSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        for ($fixtureIndex = 0; $fixtureIndex -lt $fixtureNames.Count; $fixtureIndex++) {
            Assert-NonemptyString -Value $fixtureNames[$fixtureIndex] -Label "test-fixture round manifest.names[$fixtureIndex]"
            if (-not $fixtureSeen.Add([string]$fixtureNames[$fixtureIndex])) {
                throw "test-fixture round manifest contains a duplicate name"
            }
        }
        $fixtureNameBytes = [Text.UTF8Encoding]::new($false).GetBytes([string]::Join("`n", $fixtureNames))
        $script:ExpectedRoundScenarioNamesSha256 = Get-BytesSha256 -Bytes $fixtureNameBytes
        $sourcePaths["testFixtureRoundManifest"] = $fixtureManifestPath
    }
    # Static import closure rooted at the production verifier/manager/environment.
    # AdditionalSourceFile deliberately remains fixture-only and cannot be used to
    # repair an incomplete production attestation set.
    $productionDependencies = @()
    if (-not $testFixtureMode) {
        $productionDependencies = @(
            (Join-Path $ProjectRoot "agent\__init__.py"),
            (Join-Path $ProjectRoot "agent\action_outcomes.py"),
            (Join-Path $ProjectRoot "agent\action_policy.py"),
            (Join-Path $ProjectRoot "agent\approval_receipt.py"),
            (Join-Path $ProjectRoot "agent\advanced_actions.py"),
            (Join-Path $ProjectRoot "agent\candidates.py"),
            (Join-Path $ProjectRoot "agent\canonical_hash.py"),
            (Join-Path $ProjectRoot "agent\command_custody.py"),
            (Join-Path $ProjectRoot "agent\command_queue.py"),
            (Join-Path $ProjectRoot "agent\control_packet.py"),
            (Join-Path $ProjectRoot "agent\clone_launch_guard.py"),
            (Join-Path $ProjectRoot "agent\colony_plan.py"),
            (Join-Path $ProjectRoot "agent\controller_bridge.py"),
            (Join-Path $ProjectRoot "agent\controller_loop.py"),
            (Join-Path $ProjectRoot "agent\decision.py"),
            (Join-Path $ProjectRoot "agent\decision_frame_archive.py"),
            (Join-Path $ProjectRoot "agent\field_contracts.py"),
            (Join-Path $ProjectRoot "agent\incidents.py"),
            (Join-Path $ProjectRoot "agent\llm_planner.py"),
            (Join-Path $ProjectRoot "agent\model_loop.py"),
            (Join-Path $ProjectRoot "agent\plan_coordinator.py"),
            (Join-Path $ProjectRoot "agent\rules.py"),
            (Join-Path $ProjectRoot "agent\runtime_confirmation.py"),
            (Join-Path $ProjectRoot "agent\state_quality.py"),
            (Join-Path $ProjectRoot "agent\strategy.py"),
            (Join-Path $ProjectRoot "bridge\oni_bridge.py"),
            (Join-Path $PSScriptRoot "compare_live_safety_boundaries.py"),
            (Join-Path $PSScriptRoot "configure_live_validation_runtime.py"),
            (Join-Path $PSScriptRoot "create_shutdown_evidence.py"),
            (Join-Path $PSScriptRoot "create_startup_consumption_evidence.py"),
            (Join-Path $PSScriptRoot "finalize_priority_pair_lineage.py"),
            (Join-Path $PSScriptRoot "manage_live_validation_environment.py"),
            (Join-Path $PSScriptRoot "manage_save_lineage.py"),
            (Join-Path $PSScriptRoot "prepare-v27-runtime.ps1")
        )
    }
    $dependencyIndex = 0
    foreach ($dependency in @($productionDependencies + $AdditionalSourceFile)) {
        if ([string]::IsNullOrWhiteSpace($dependency)) { continue }
        $resolvedDependency = Resolve-NormalizedPath -Path $dependency -Base $ProjectRoot
        Assert-LocalCanonicalPathSyntax -Path $resolvedDependency -Label "attested dependency"
        if (-not (Test-Path -LiteralPath $resolvedDependency -PathType Leaf)) { throw "attested dependency is missing: $resolvedDependency" }
        Assert-NoReparseSegments -Path $resolvedDependency
        if ($resolvedDependency -ieq $canonicalPath) { throw "canonical report overlaps an attested dependency" }
        if ($testFixtureMode -and -not (Test-PathWithinRoot -Path $resolvedDependency -Root $resolvedTestRoot)) {
            throw "fixture dependency must be contained by TestRoot: $resolvedDependency"
        }
        if ($sourcePaths.Values -inotcontains $resolvedDependency) {
            $sourcePaths["dependency_$dependencyIndex"] = $resolvedDependency
            $dependencyIndex++
        }
    }

    $executionRootCandidate = Get-FreshOwnedExecutionRootCandidate
    $executionRoot = [string]$executionRootCandidate.path
    $executionTempRoot = [string]$executionRootCandidate.tempRoot
    $executionTempRootIdentityBefore = [string]$executionRootCandidate.tempRootIdentity
    [void](New-Item -ItemType Directory -Path $executionRoot -ErrorAction Stop)
    $createdExecutionRoot = Get-Item -LiteralPath $executionRoot -Force
    $executionTempRootIdentityAfter = ([OniRunner.NativeFileIdentity]::Read($executionTempRoot, $true)).Identity
    $executionRootIdentity = ([OniRunner.NativeFileIdentity]::Read($executionRoot, $true)).Identity
    if (
        -not $createdExecutionRoot.PSIsContainer -or
        [IO.Path]::GetFullPath($createdExecutionRoot.FullName) -ine $executionRoot -or
        $executionTempRootIdentityAfter -cne $executionTempRootIdentityBefore -or
        (($createdExecutionRoot.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    ) {
        throw "execution root was not the expected plain owned directory"
    }
    Assert-NoReparseSegments -Path $executionRoot
    $stdoutLog = Join-Path $executionRoot "worker.stdout.log"
    $stderrLog = Join-Path $executionRoot "worker.stderr.log"
    $workerReportPath = Join-Path $executionRoot "worker-live-runtime-bootstrap-verification.json"
    $workerAttestationPath = Join-Path $executionRoot "worker-pycache-attestation.json"
    $pycachePrefix = Join-Path $executionRoot "pycache"
    if (Test-Path -LiteralPath $pycachePrefix) { throw "run-scoped pycache prefix was not fresh" }
    [void](New-Item -ItemType Directory -Path $pycachePrefix)
    Assert-NoReparseSegments -Path $pycachePrefix
    if (@(Get-ChildItem -LiteralPath $pycachePrefix -Force).Count -ne 0) {
        throw "run-scoped pycache prefix was not empty after creation"
    }

    $sourceLocks = Open-LockedSources -SourcePaths $sourcePaths
    $preSourceHashes = $sourceLocks.summaries
    $preCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true

    $pythonSourcePaths = @(
        $sourcePaths.Values |
            Where-Object { [IO.Path]::GetExtension([string]$_) -ieq ".py" } |
            ForEach-Object { [IO.Path]::GetFullPath([string]$_) } |
            Sort-Object -Unique
    )
    $moduleRoots = @([IO.Path]::GetFullPath($ProjectRoot))
    if ($testFixtureMode) { $moduleRoots += [IO.Path]::GetFullPath($resolvedTestRoot) }
    $requiredLoadedSources = @($workerPath, $verifierPath, $managerPath)
    if (-not $testFixtureMode) {
        $requiredLoadedSources += [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "manage_live_validation_environment.py"))
    }

    $pythonArguments = @()
    $pythonArguments += @(
        "-I", "-B", "-X", "utf8", "-X", "pycache_prefix=$pycachePrefix", "-u", $workerPath,
        "--verifier", $verifierPath,
        "--manager", $managerPath,
        "--rounds", "2",
        "--report", $workerReportPath,
        "--pycache-prefix", $pycachePrefix,
        "--attestation-report", $workerAttestationPath
    )
    foreach ($pythonSourcePath in $pythonSourcePaths) {
        $pythonArguments += @("--attested-source", $pythonSourcePath)
    }
    foreach ($moduleRoot in $moduleRoots) {
        $pythonArguments += @("--module-root", $moduleRoot)
    }
    foreach ($requiredLoadedSource in $requiredLoadedSources) {
        $pythonArguments += @("--required-loaded-source", $requiredLoadedSource)
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $pythonPath
    $startInfo.WorkingDirectory = $ProjectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($unsafePythonEnvironmentName in @("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")) {
        $startInfo.EnvironmentVariables.Remove($unsafePythonEnvironmentName)
    }
    $startInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
    # The isolated parent ignores Python environment configuration, so its
    # prefix is explicit in argv. Manager subprocesses do not use -I and inherit
    # this matching prefix plus the no-write flag.
    $startInfo.EnvironmentVariables["PYTHONPYCACHEPREFIX"] = $pycachePrefix
    $startInfo.EnvironmentVariables["PYTHONNOUSERSITE"] = "1"
    $startInfo.EnvironmentVariables["PYTHONSAFEPATH"] = "1"
    $startInfo.EnvironmentVariables["PYTHONUTF8"] = "1"
    if ($null -ne $startInfo.PSObject.Properties["ArgumentList"]) {
        foreach ($argument in $pythonArguments) { [void]$startInfo.ArgumentList.Add([string]$argument) }
    } else {
        $startInfo.Arguments = (@($pythonArguments | ForEach-Object { ConvertTo-NativeArgument -Argument ([string]$_) }) -join " ")
    }

    $stdoutStream = $null
    $stderrStream = $null
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $stdoutCopyTask = $null
    $stderrCopyTask = $null
    $stdoutBoundedStream = $null
    $stderrBoundedStream = $null
    $processStarted = $false
    $workerJob = $null
    $workerFailure = $null
    try {
        $stdoutStream = [IO.FileStream]::new($stdoutLog, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $stderrStream = [IO.FileStream]::new($stderrLog, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $stdoutBoundedStream = [OniRunner.BoundedWriteStream]::new($stdoutStream, $script:MaximumStdoutBytes)
        $stderrBoundedStream = [OniRunner.BoundedWriteStream]::new($stderrStream, $script:MaximumStderrBytes)
        $workerJob = [OniRunner.KillOnCloseJob]::new()
        if (-not $process.Start()) { throw "worker process did not start" }
        $processStarted = $true
        $workerJob.Assign($process)
        $jobAssigned = $true
        $workerProcessId = [int]$process.Id
        $stdoutCopyTask = $process.StandardOutput.BaseStream.CopyToAsync($stdoutBoundedStream)
        $stderrCopyTask = $process.StandardError.BaseStream.CopyToAsync($stderrBoundedStream)
        Send-Heartbeat -Stage "started" -ProcessId $workerProcessId -Elapsed $watch.Elapsed.TotalSeconds -Count $heartbeatCount -WorkerReport $workerReportPath -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerExitCode $null
        $heartbeatCount++

        $pollMilliseconds = [int][Math]::Min(1000, [Math]::Ceiling($HeartbeatSeconds * 1000))
        $nextHeartbeatAt = $watch.Elapsed.TotalSeconds + $HeartbeatSeconds
        while (-not $process.WaitForExit($pollMilliseconds)) {
            $captureFailure = Get-CaptureFailure -Task $stdoutCopyTask -BoundedStream $stdoutBoundedStream -Label "worker stdout" -MaximumBytes $script:MaximumStdoutBytes
            if ([string]::IsNullOrWhiteSpace($captureFailure)) {
                $captureFailure = Get-CaptureFailure -Task $stderrCopyTask -BoundedStream $stderrBoundedStream -Label "worker stderr" -MaximumBytes $script:MaximumStderrBytes
            }
            if (-not [string]::IsNullOrWhiteSpace($captureFailure)) {
                $resourceLimitError = $captureFailure
                throw $captureFailure
            }
            $resourceState = Get-WorkerResourceState -ExecutionRoot $executionRoot -ExpectedExecutionRootIdentity $executionRootIdentity -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerReport $workerReportPath -Watch $watch -DeadlineSeconds $TotalTimeoutSeconds
            $stdoutBytesObserved = [long][Math]::Max($stdoutBytesObserved, $resourceState.stdoutBytes)
            $stderrBytesObserved = [long][Math]::Max($stderrBytesObserved, $resourceState.stderrBytes)
            $reportBytesObserved = [long][Math]::Max($reportBytesObserved, $resourceState.reportBytes)
            $executionRootBytesObserved = [long][Math]::Max($executionRootBytesObserved, $resourceState.executionRootBytes)
            $resourceEntriesObserved = [long]$resourceState.executionRootEntries
            if ($resourceEntriesObserved -gt $executionRootEntriesObserved) {
                $executionRootEntriesObserved = $resourceEntriesObserved
                $executionRootFilesObserved = [long]$resourceState.executionRootFiles
                $executionRootDirectoriesObserved = [long]$resourceState.executionRootDirectories
            }
            $executionRootScanRestartsObserved = [long][Math]::Max($executionRootScanRestartsObserved, $resourceState.executionRootScanRestarts)
            $executionRootTransientDisappearancesObserved = [long][Math]::Max($executionRootTransientDisappearancesObserved, $resourceState.executionRootTransientDisappearances)
            if (-not [string]::IsNullOrWhiteSpace($resourceState.violation)) {
                $resourceLimitError = $resourceState.violation
                throw $resourceLimitError
            }
            if ($watch.Elapsed.TotalSeconds -ge $nextHeartbeatAt) {
                Send-Heartbeat -Stage "running" -ProcessId $workerProcessId -Elapsed $watch.Elapsed.TotalSeconds -Count $heartbeatCount -WorkerReport $workerReportPath -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerExitCode $null
                $heartbeatCount++
                do { $nextHeartbeatAt += $HeartbeatSeconds } while ($nextHeartbeatAt -le $watch.Elapsed.TotalSeconds)
            }
        }
        $process.WaitForExit()
        $workerExitCode = [int]$process.ExitCode
        $captureFailure = Get-CaptureFailure -Task $stdoutCopyTask -BoundedStream $stdoutBoundedStream -Label "worker stdout" -MaximumBytes $script:MaximumStdoutBytes
        if ([string]::IsNullOrWhiteSpace($captureFailure)) {
            $captureFailure = Get-CaptureFailure -Task $stderrCopyTask -BoundedStream $stderrBoundedStream -Label "worker stderr" -MaximumBytes $script:MaximumStderrBytes
        }
        if (-not [string]::IsNullOrWhiteSpace($captureFailure)) {
            $resourceLimitError = $captureFailure
            throw $captureFailure
        }
        $resourceState = Get-WorkerResourceState -ExecutionRoot $executionRoot -ExpectedExecutionRootIdentity $executionRootIdentity -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerReport $workerReportPath -Watch $watch -DeadlineSeconds $TotalTimeoutSeconds
        $stdoutBytesObserved = [long][Math]::Max($stdoutBytesObserved, $resourceState.stdoutBytes)
        $stderrBytesObserved = [long][Math]::Max($stderrBytesObserved, $resourceState.stderrBytes)
        $reportBytesObserved = [long][Math]::Max($reportBytesObserved, $resourceState.reportBytes)
        $executionRootBytesObserved = [long][Math]::Max($executionRootBytesObserved, $resourceState.executionRootBytes)
        $resourceEntriesObserved = [long]$resourceState.executionRootEntries
        if ($resourceEntriesObserved -gt $executionRootEntriesObserved) {
            $executionRootEntriesObserved = $resourceEntriesObserved
            $executionRootFilesObserved = [long]$resourceState.executionRootFiles
            $executionRootDirectoriesObserved = [long]$resourceState.executionRootDirectories
        }
        $executionRootScanRestartsObserved = [long][Math]::Max($executionRootScanRestartsObserved, $resourceState.executionRootScanRestarts)
        $executionRootTransientDisappearancesObserved = [long][Math]::Max($executionRootTransientDisappearancesObserved, $resourceState.executionRootTransientDisappearances)
        if (-not [string]::IsNullOrWhiteSpace($resourceState.violation)) {
            $resourceLimitError = $resourceState.violation
            throw $resourceLimitError
        }
    } catch {
        $workerFailure = "{0} [script line {1}]" -f $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber
    } finally {
        Stop-WorkerProcessTree -Process $process -Started $processStarted
        if ($null -ne $workerJob -and $jobAssigned) {
            try {
                $jobActiveBeforeCleanup = [long]$workerJob.ActiveProcessCount()
                $jobCleanupSucceeded = [bool]$workerJob.TerminateAndWait(5000)
                $jobActiveAfterCleanup = [long]$workerJob.ActiveProcessCount()
            } catch {
                $jobCleanupSucceeded = $false
                $cleanupError = "job cleanup failed: $($_.Exception.Message)"
            }
        }
        if ($TestInjectJobCleanupFailure) {
            $jobCleanupSucceeded = $false
            $cleanupError = "fixture injected job cleanup failure"
        }
        if ($processStarted) {
            try { [void]$process.WaitForExit(5000) } catch { }
        }
        if ($null -ne $stdoutCopyTask -and $null -ne $stderrCopyTask) {
            try {
                $drainTasks = [Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]@($stdoutCopyTask, $stderrCopyTask))
                if (-not $drainTasks.Wait(5000)) { throw "stdout/stderr copy tasks did not drain within 5 seconds" }
                [void]$drainTasks.GetAwaiter().GetResult()
                $copyDrainCompleted = $true
            } catch {
                $copyDrainCompleted = $false
                if ([string]::IsNullOrWhiteSpace($cleanupError)) {
                    $cleanupError = "copy drain failed: $($_.Exception.Message)"
                } else {
                    $cleanupError += "; copy drain failed: $($_.Exception.Message)"
                }
            }
        }
        if ($null -ne $stdoutBoundedStream) {
            $stdoutBytesObserved = [long][Math]::Max($stdoutBytesObserved, $stdoutBoundedStream.ObservedBytes)
            $stdoutLogTruncated = [bool]$stdoutBoundedStream.LimitExceeded
        }
        if ($null -ne $stderrBoundedStream) {
            $stderrBytesObserved = [long][Math]::Max($stderrBytesObserved, $stderrBoundedStream.ObservedBytes)
            $stderrLogTruncated = [bool]$stderrBoundedStream.LimitExceeded
        }
        if ($null -ne $stdoutStream) {
            try { $stdoutStream.Flush($true) } catch { $cleanupError = "stdout flush failed: $($_.Exception.Message)" }
            $stdoutStream.Dispose()
        }
        if ($null -ne $stderrStream) {
            try { $stderrStream.Flush($true) } catch { $cleanupError = "stderr flush failed: $($_.Exception.Message)" }
            $stderrStream.Dispose()
        }
        try {
            $stdoutLimit = Limit-LogFile -Path $stdoutLog -MaximumBytes $script:MaximumStdoutBytes
            $stderrLimit = Limit-LogFile -Path $stderrLog -MaximumBytes $script:MaximumStderrBytes
            $stdoutBytesObserved = [long][Math]::Max($stdoutBytesObserved, $stdoutLimit.observedBytes)
            $stderrBytesObserved = [long][Math]::Max($stderrBytesObserved, $stderrLimit.observedBytes)
            $stdoutLogTruncated = $stdoutLogTruncated -or [bool]$stdoutLimit.truncated
            $stderrLogTruncated = $stderrLogTruncated -or [bool]$stderrLimit.truncated
        } catch {
            $cleanupError = "log cap enforcement failed: $($_.Exception.Message)"
        }
        if ($null -ne $workerJob) {
            try { $workerJob.Dispose() } catch {
                $jobCleanupSucceeded = $false
                $cleanupError = "job disposal failed: $($_.Exception.Message)"
            }
        }
        $process.Dispose()
    }

    $cleanupProblems = [Collections.Generic.List[string]]::new()
    if (-not $jobAssigned) { [void]$cleanupProblems.Add("worker was not assigned to the kill-on-close job") }
    if (-not $jobCleanupSucceeded) { [void]$cleanupProblems.Add("job cleanup did not succeed") }
    if ($jobActiveAfterCleanup -ne 0) { [void]$cleanupProblems.Add("job still had active processes after cleanup") }
    if (-not $copyDrainCompleted) { [void]$cleanupProblems.Add("stdout/stderr copy did not drain") }
    if (-not [string]::IsNullOrWhiteSpace($cleanupError)) { [void]$cleanupProblems.Add($cleanupError) }
    if ($cleanupProblems.Count -gt 0) {
        $cleanupGateError = "worker cleanup gate failed: " + [string]::Join("; ", $cleanupProblems)
        if ([string]::IsNullOrWhiteSpace($workerFailure)) { $workerFailure = $cleanupGateError } else { $workerFailure += "; $cleanupGateError" }
    }
    if (-not [string]::IsNullOrWhiteSpace($workerFailure)) { throw $workerFailure }

    Send-Heartbeat -Stage "completed" -ProcessId $workerProcessId -Elapsed $watch.Elapsed.TotalSeconds -Count $heartbeatCount -WorkerReport $workerReportPath -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerExitCode $workerExitCode
    $heartbeatCount++

    # Integrity checks run even when the worker failed. A non-zero exit must not
    # hide concurrent source or canonical drift.
    $postSourceHashes = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "worker execution"
    $prePublishCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
    Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $prePublishCanonicalSnapshot -Stage "worker execution"
    if (Test-Path -LiteralPath $workerReportPath -PathType Leaf) {
        try {
            $failedCandidate = Read-FileSnapshot -Path $workerReportPath
            $candidateRawSha256 = $failedCandidate.sha256
            $failedDocument = ConvertFrom-StrictUtf8JsonBytes -Bytes $failedCandidate.bytes -Label "worker report"
            $failedReportSummary = [ordered]@{
                schema = if ($null -ne $failedDocument.PSObject.Properties["schema"]) { Get-BoundedText -Value $failedDocument.schema } else { $null }
                status = if ($null -ne $failedDocument.PSObject.Properties["status"]) { Get-BoundedText -Value $failedDocument.status } else { $null }
                failure = if ($null -ne $failedDocument.PSObject.Properties["failure"]) { Get-BoundedText -Value $failedDocument.failure } else { $null }
                preservedTestRoot = if ($null -ne $failedDocument.PSObject.Properties["preserved_test_root"]) { Get-BoundedText -Value $failedDocument.preserved_test_root } else { $null }
                rawSha256 = $failedCandidate.sha256
                parseError = $null
            }
        } catch {
            $failedReportSummary = [ordered]@{ parseError = Get-BoundedText -Value $_.Exception.Message }
        }
    }
    if ($workerExitCode -ne 0) { throw "worker returned non-zero exit code: $workerExitCode" }
    if (-not (Test-Path -LiteralPath $workerAttestationPath -PathType Leaf)) {
        throw "worker returned success without a pycache attestation"
    }
    $attestationSnapshot = Read-FileSnapshot -Path $workerAttestationPath
    $workerPycacheAttestationRawSha256 = $attestationSnapshot.sha256
    $workerPycacheAttestationRawLength = $attestationSnapshot.length
    $attestationDocument = ConvertFrom-StrictUtf8JsonBytes -Bytes $attestationSnapshot.bytes -Label "worker pycache attestation"
    $workerPycacheAttestation = Assert-PycacheAttestation `
        -Document $attestationDocument `
        -ExpectedPrefix $pycachePrefix `
        -ExpectedSources $pythonSourcePaths `
        -RequiredLoadedSources $requiredLoadedSources `
        -ModuleRoots $moduleRoots
    Assert-NoReparseSegments -Path $pycachePrefix
    if (@(Get-ChildItem -LiteralPath $pycachePrefix -Force -Recurse).Count -ne 0) {
        throw "run-scoped pycache prefix was not empty after worker exit"
    }
    if (-not (Test-Path -LiteralPath $workerReportPath -PathType Leaf)) { throw "worker returned success without a report" }

    # This locked raw-byte snapshot is the only candidate content ever written
    # to the adjacent publish temp. The worker report is never re-read for copy.
    $candidateSnapshot = Read-FileSnapshot -Path $workerReportPath
    $candidateRawSha256 = $candidateSnapshot.sha256
    $candidateReport = ConvertFrom-StrictUtf8JsonBytes -Bytes $candidateSnapshot.bytes -Label "worker report"
    $validatedSummary = Assert-VerificationReport -Report $candidateReport
    $postSourceHashes = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "candidate validation"
    $prePublishCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
    Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $prePublishCanonicalSnapshot -Stage "candidate validation"

    if (-not $WhatIf) {
        $canonicalParent = Split-Path -Parent $canonicalPath
        if (-not (Test-Path -LiteralPath $canonicalParent -PathType Container)) {
            throw "canonical publish parent must already exist: $canonicalParent"
        }
        $publishParentState = Get-PublishParentState -Path $canonicalParent
        $publishLockPath = Join-Path $canonicalParent ("." + [IO.Path]::GetFileName($canonicalPath) + ".publish.lock")
        $adjacentTemp = Join-Path $canonicalParent ("." + [IO.Path]::GetFileName($canonicalPath) + "." + [guid]::NewGuid().ToString("N") + ".publish.tmp")
        $backupPath = Join-Path $canonicalParent ("." + [IO.Path]::GetFileName($canonicalPath) + "." + [guid]::NewGuid().ToString("N") + ".rollback.bak")
        $destinationExisted = [bool]$preCanonicalSnapshot.exists
        try {
            $publishLockStream = [IO.FileStream]::new(
                $publishLockPath,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None,
                4096,
                [IO.FileOptions]::WriteThrough
            )
            $lockBytes = [Text.UTF8Encoding]::new($false).GetBytes("pid=$PID action=run-live-runtime-bootstrap-verification`n")
            $publishLockExpectedSha256 = Get-BytesSha256 -Bytes $lockBytes
            $publishLockStream.Write($lockBytes, 0, $lockBytes.Length)
            $publishLockStream.Flush($true)
            $publishLockHandleIdentity = [OniRunner.NativeFileIdentity]::ReadHandle($publishLockStream.SafeFileHandle).Identity
            Assert-SamePublishParent -Expected $publishParentState -Observed (Get-PublishParentState -Path $canonicalParent)
            $lockedCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
            Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $lockedCanonicalSnapshot -Stage "publish-lock acquisition"

            Write-BytesCreateNew -Path $adjacentTemp -Bytes $candidateSnapshot.bytes
            $tempSnapshot = Read-FileSnapshot -Path $adjacentTemp
            $tempRawSha256 = $tempSnapshot.sha256
            if (-not (Test-ByteArrayEqual -Left $candidateSnapshot.bytes -Right $tempSnapshot.bytes)) {
                throw "adjacent publish temp bytes differ from the validated candidate"
            }
            $tempReport = ConvertFrom-StrictUtf8JsonBytes -Bytes $tempSnapshot.bytes -Label "adjacent publish temp"
            [void](Assert-VerificationReport -Report $tempReport)

            $postSourceHashes = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "immediate pre-publish check"
            Assert-SamePublishParent -Expected $publishParentState -Observed (Get-PublishParentState -Path $canonicalParent)
            $prePublishCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
            Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $prePublishCanonicalSnapshot -Stage "immediate pre-publish check"

            if ($destinationExisted) {
                [IO.File]::Replace($adjacentTemp, $canonicalPath, $backupPath, $true)
            } else {
                [IO.File]::Move($adjacentTemp, $canonicalPath)
            }
            $publishMutationDone = $true
            $postCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath
            $publishedCanonicalSnapshot = $postCanonicalSnapshot
            $publishedRawSha256 = $postCanonicalSnapshot.sha256
            if ($destinationExisted) {
                $backupSnapshot = Read-FileSnapshot -Path $backupPath
                Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $backupSnapshot -Stage "post-replace backup verification"
            }
            if (-not (Test-ByteArrayEqual -Left $candidateSnapshot.bytes -Right $postCanonicalSnapshot.bytes)) {
                throw "published canonical bytes differ from the validated candidate"
            }
            $publishedReport = ConvertFrom-StrictUtf8JsonBytes -Bytes $postCanonicalSnapshot.bytes -Label "published canonical report"
            [void](Assert-VerificationReport -Report $publishedReport)
            $postSourceHashes = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "post-publish check"
            Assert-SamePublishParent -Expected $publishParentState -Observed (Get-PublishParentState -Path $canonicalParent)
            if ($TestInjectRollbackIdentitySwap) {
                $interferencePath = Join-Path $canonicalParent ("." + [IO.Path]::GetFileName($canonicalPath) + "." + [guid]::NewGuid().ToString("N") + ".interference")
                [IO.File]::Move($canonicalPath, $interferencePath)
                Write-BytesCreateNew -Path $canonicalPath -Bytes $candidateSnapshot.bytes
                throw "fixture injected same-bytes different-identity rollback interference"
            }
            if ($TestInjectPostPublishFailure) { throw "fixture injected post-publish failure" }
            $publishPendingCommit = $true
        } catch {
            $publishError = Get-BoundedText -Value $_.Exception.Message
            $reportPublished = $false
            if ($publishMutationDone) {
                $rollbackEvidence = Invoke-PublishRollback -CanonicalPath $canonicalPath -PreSnapshot $preCanonicalSnapshot -PublishedSnapshot $publishedCanonicalSnapshot -BackupPath $backupPath -DestinationExisted $destinationExisted
                if ($rollbackEvidence.succeeded) {
                    $publishRollbackCompleted = $true
                } else {
                    $publishError = Get-BoundedText -Value ($publishError + "; rollback failed: " + $rollbackEvidence.error)
                }
            }
            try { $postCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true } catch { $postCanonicalSnapshot = $null }
            throw $publishError
        } finally {
            if (Test-Path -LiteralPath $adjacentTemp -PathType Leaf) {
                try { Remove-Item -LiteralPath $adjacentTemp -Force } catch { }
            }
        }
    }

    $mainCompleted = $true
    $message = if ($WhatIf) { "verification passed; WhatIf suppressed canonical publication" } else { "verification passed and canonical report was atomically published" }
} catch {
    $primaryFailure = Get-BoundedText -Value ("{0} [script line {1}]" -f $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber)
    $message = $primaryFailure
} finally {
    # The independent pre-commit audits both participate in the commit/rollback
    # decision. Source handles remain open for a second audit after the chosen
    # publication transaction reaches its terminal state.
    if ($null -ne $sourceLocks -and $null -ne $preSourceHashes) {
        try {
            $sourceAuditObserved = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "pre-commit post-audit"
            if ($TestInjectSourceAuditFailure) { throw "fixture injected source post-audit failure" }
            $postSourceHashes = $sourceAuditObserved
        } catch {
            $sourceAuditError = Get-BoundedText -Value $_.Exception.Message
        }
    }
    if ($null -ne $preCanonicalSnapshot -and -not [string]::IsNullOrWhiteSpace($canonicalPath)) {
        try {
            $canonicalAuditObserved = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
            if ($TestInjectCanonicalAuditFailure) { throw "fixture injected canonical post-audit failure" }
            $expectedCanonical = if ($publishPendingCommit) { $publishedCanonicalSnapshot } else { $preCanonicalSnapshot }
            Assert-SameCanonicalState -Expected $expectedCanonical -Observed $canonicalAuditObserved -Stage "final post-audit"
            $postCanonicalSnapshot = $canonicalAuditObserved
        } catch {
            $canonicalAuditError = Get-BoundedText -Value $_.Exception.Message
        }
    }
    $finalFailures = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($primaryFailure)) { [void]$finalFailures.Add($primaryFailure) }
    if (-not [string]::IsNullOrWhiteSpace($sourceAuditError)) { [void]$finalFailures.Add("source post-audit failed: $sourceAuditError") }
    if (-not [string]::IsNullOrWhiteSpace($canonicalAuditError)) { [void]$finalFailures.Add("canonical post-audit failed: $canonicalAuditError") }
    if (-not $mainCompleted -and $finalFailures.Count -eq 0) { [void]$finalFailures.Add("runner did not complete its main transaction") }
    if ($mainCompleted -and $watch.Elapsed.TotalSeconds -gt $TotalTimeoutSeconds) {
        [void]$finalFailures.Add("runner exceeded the $TotalTimeoutSeconds-second total deadline")
    }

    if ($finalFailures.Count -gt 0 -and $publishMutationDone -and -not $rollbackEvidence.attempted) {
        $rollbackEvidence = Invoke-PublishRollback -CanonicalPath $canonicalPath -PreSnapshot $preCanonicalSnapshot -PublishedSnapshot $publishedCanonicalSnapshot -BackupPath $backupPath -DestinationExisted $destinationExisted
        $publishRollbackCompleted = [bool]$rollbackEvidence.succeeded
        if (-not $rollbackEvidence.succeeded) {
            [void]$finalFailures.Add("rollback failed: $($rollbackEvidence.error)")
        } else {
            try {
                $postCanonicalSnapshot = Read-FileSnapshot -Path $canonicalPath -AllowMissing $true
                Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $postCanonicalSnapshot -Stage "final rollback recovery audit"
            } catch {
                [void]$finalFailures.Add("post-rollback canonical audit failed: $(Get-BoundedText -Value $_.Exception.Message)")
            }
        }
        $reportPublished = Test-PublishedStateIsCurrent -CanonicalPath $canonicalPath -PublishedSnapshot $publishedCanonicalSnapshot
    }

    $commitCanonical = $null
    if ($finalFailures.Count -eq 0 -and $publishPendingCommit) {
        try {
            $commitCanonical = Read-FileSnapshot -Path $canonicalPath
            Assert-SameCanonicalState -Expected $publishedCanonicalSnapshot -Observed $commitCanonical -Stage "commit ownership recheck"
            if ($destinationExisted) {
                $commitBackup = Read-FileSnapshot -Path $backupPath
                Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $commitBackup -Stage "commit backup recheck"
            }
        } catch {
            [void]$finalFailures.Add("publication commit verification failed: $(Get-BoundedText -Value $_.Exception.Message)")
        }
        if ($finalFailures.Count -gt 0 -and -not $rollbackEvidence.attempted) {
            $rollbackEvidence = Invoke-PublishRollback -CanonicalPath $canonicalPath -PreSnapshot $preCanonicalSnapshot -PublishedSnapshot $publishedCanonicalSnapshot -BackupPath $backupPath -DestinationExisted $destinationExisted
            $publishRollbackCompleted = [bool]$rollbackEvidence.succeeded
            $reportPublished = Test-PublishedStateIsCurrent -CanonicalPath $canonicalPath -PublishedSnapshot $publishedCanonicalSnapshot
            if (-not $rollbackEvidence.succeeded) { [void]$finalFailures.Add("rollback failed: $($rollbackEvidence.error)") }
        }
    }

    if ($finalFailures.Count -eq 0 -and $publishPendingCommit) {
        try {
            $publishedCanonicalBridgeStream = [IO.FileStream]::new(
                $canonicalPath,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read
            )
            $bridgeSnapshot = Read-OpenFileSnapshot -Path $canonicalPath -Stream $publishedCanonicalBridgeStream
            Assert-SameCanonicalState -Expected $commitCanonical -Observed $bridgeSnapshot -Stage "canonical bridge acquisition"
            $publishedCanonicalBridgeHeld = $true
        } catch {
            $publishedCanonicalBridgeError = Get-BoundedText -Value $_.Exception.Message
            [void]$finalFailures.Add("canonical bridge acquisition failed: $publishedCanonicalBridgeError")
            if ($null -ne $publishedCanonicalBridgeStream) {
                try { $publishedCanonicalBridgeStream.Dispose() } catch { }
                $publishedCanonicalBridgeStream = $null
            }
        }
    }

    # Release the kernel handle while the verified rollback backup is retained.
    # The persistent, identity-bound lock marker bridges the handle release to
    # backup cleanup and prevents another cooperative publisher from entering.
    if ($null -ne $publishLockStream) {
        try {
            $publishLockStream.Dispose()
            if ($TestInjectPublishLockReleaseFailure) { throw "fixture injected publish-lock release failure" }
            $publishLockReleased = $true
        } catch {
            $publishLockReleaseError = Get-BoundedText -Value $_.Exception.Message
            [void]$finalFailures.Add("publish-lock release failed: $publishLockReleaseError")
        }
    }
    if ($finalFailures.Count -gt 0 -and $publishMutationDone -and -not $rollbackEvidence.attempted) {
        if ($null -ne $publishedCanonicalBridgeStream) {
            try { $publishedCanonicalBridgeStream.Dispose() } catch {
                [void]$finalFailures.Add("canonical bridge release before rollback failed: $(Get-BoundedText -Value $_.Exception.Message)")
            }
            $publishedCanonicalBridgeStream = $null
        }
        $rollbackEvidence = Invoke-PublishRollback -CanonicalPath $canonicalPath -PreSnapshot $preCanonicalSnapshot -PublishedSnapshot $publishedCanonicalSnapshot -BackupPath $backupPath -DestinationExisted $destinationExisted
        $publishRollbackCompleted = [bool]$rollbackEvidence.succeeded
        if (-not $rollbackEvidence.succeeded) { [void]$finalFailures.Add("rollback failed: $($rollbackEvidence.error)") }
        $reportPublished = Test-PublishedStateIsCurrent -CanonicalPath $canonicalPath -PublishedSnapshot $publishedCanonicalSnapshot
    }

    if ($finalFailures.Count -eq 0 -and $publishPendingCommit) {
        try {
            if (-not $publishLockReleased) { throw "publish-lock release was not confirmed" }
            $releasedLockSnapshot = Read-FileSnapshot -Path $publishLockPath
            if (
                $releasedLockSnapshot.fileIdentity -cne $publishLockHandleIdentity -or
                $releasedLockSnapshot.sha256 -cne $publishLockExpectedSha256
            ) {
                throw "persistent publish-lock marker identity or bytes changed"
            }
            if ($null -eq $publishedCanonicalBridgeStream) { throw "canonical bridge handle was not held" }
            $finalCommitCanonical = Read-OpenFileSnapshot -Path $canonicalPath -Stream $publishedCanonicalBridgeStream
            Assert-SameCanonicalState -Expected $commitCanonical -Observed $finalCommitCanonical -Stage "final commit ownership check"
            if ($destinationExisted) {
                $finalCommitBackup = Read-FileSnapshot -Path $backupPath
                Assert-SameCanonicalState -Expected $preCanonicalSnapshot -Observed $finalCommitBackup -Stage "final commit backup check"
                Remove-Item -LiteralPath $backupPath -Force
                if (Test-Path -LiteralPath $backupPath) { throw "rollback backup remained after commit cleanup" }
            }
            $postCanonicalSnapshot = Read-OpenFileSnapshot -Path $canonicalPath -Stream $publishedCanonicalBridgeStream
            Assert-SameCanonicalState -Expected $finalCommitCanonical -Observed $postCanonicalSnapshot -Stage "post-backup-cleanup commit check"
            $reportPublished = $true
        } catch {
            [void]$finalFailures.Add("publication commit failed: $(Get-BoundedText -Value $_.Exception.Message)")
            if (-not $rollbackEvidence.attempted) {
                if ($null -ne $publishedCanonicalBridgeStream) {
                    try { $publishedCanonicalBridgeStream.Dispose() } catch {
                        [void]$finalFailures.Add("canonical bridge release before rollback failed: $(Get-BoundedText -Value $_.Exception.Message)")
                    }
                    $publishedCanonicalBridgeStream = $null
                }
                $rollbackEvidence = Invoke-PublishRollback -CanonicalPath $canonicalPath -PreSnapshot $preCanonicalSnapshot -PublishedSnapshot $publishedCanonicalSnapshot -BackupPath $backupPath -DestinationExisted $destinationExisted
                $publishRollbackCompleted = [bool]$rollbackEvidence.succeeded
                if (-not $rollbackEvidence.succeeded) { [void]$finalFailures.Add("rollback failed: $($rollbackEvidence.error)") }
            }
            $reportPublished = Test-PublishedStateIsCurrent -CanonicalPath $canonicalPath -PublishedSnapshot $publishedCanonicalSnapshot
        }
    }

    if ($null -ne $sourceLocks -and $null -ne $preSourceHashes) {
        try {
            $sourceAuditObserved = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "post-commit pre-release-tail audit"
            $postSourceHashes = $sourceAuditObserved
        } catch {
            $finalSourceAuditError = Get-BoundedText -Value $_.Exception.Message
            if ([string]::IsNullOrWhiteSpace($sourceAuditError)) { $sourceAuditError = $finalSourceAuditError }
            [void]$finalFailures.Add("post-commit source audit failed: $finalSourceAuditError")
        }
    }

    # Best-effort resource cleanup never erases the earlier release failure. The
    # marker is removed only when it still has the identity/bytes created here.
    if ($null -ne $publishLockStream) {
        try { $publishLockStream.Dispose() } catch {
            if ([string]::IsNullOrWhiteSpace($publishLockCleanupError)) {
                $publishLockCleanupError = Get-BoundedText -Value ("publish-lock retry disposal failed: " + $_.Exception.Message)
            }
        }
        $publishLockStream = $null
    }
    if (-not [string]::IsNullOrWhiteSpace($publishLockHandleIdentity) -and (Test-Path -LiteralPath $publishLockPath -PathType Leaf)) {
        try {
            $lockCleanupSnapshot = Read-FileSnapshot -Path $publishLockPath
            if (
                $lockCleanupSnapshot.fileIdentity -cne $publishLockHandleIdentity -or
                $lockCleanupSnapshot.sha256 -cne $publishLockExpectedSha256
            ) {
                throw "publish-lock marker no longer belongs to this run"
            }
            Remove-Item -LiteralPath $publishLockPath -Force
            if (Test-Path -LiteralPath $publishLockPath) { throw "publish-lock marker remained after cleanup" }
        } catch {
            $publishLockCleanupError = Get-BoundedText -Value $_.Exception.Message
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($publishLockCleanupError)) {
        [void]$finalFailures.Add("publish-lock cleanup failed: $publishLockCleanupError")
    }
    if ($null -ne $publishedCanonicalBridgeStream) {
        try {
            $finalBridgeSnapshot = Read-OpenFileSnapshot -Path $canonicalPath -Stream $publishedCanonicalBridgeStream
            Assert-SameCanonicalState -Expected $publishedCanonicalSnapshot -Observed $finalBridgeSnapshot -Stage "canonical bridge final release"
            $publishedCanonicalBridgeStream.Dispose()
        } catch {
            $publishedCanonicalBridgeError = Get-BoundedText -Value $_.Exception.Message
            [void]$finalFailures.Add("canonical bridge final release failed: $publishedCanonicalBridgeError")
            try { $publishedCanonicalBridgeStream.Dispose() } catch { }
        }
        $publishedCanonicalBridgeStream = $null
    }
    $publicationTransactionCompleted = $true

    # The terminal audit and optional write-denial probe happen only after the
    # marker and canonical bridge release tail. Handles close last.
    if ($null -ne $sourceLocks -and $null -ne $preSourceHashes) {
        try {
            $sourceAuditObserved = Assert-LockedSources -LockedSources $sourceLocks -Expected $preSourceHashes -Stage "terminal post-transaction audit"
            $postSourceHashes = $sourceAuditObserved
        } catch {
            $terminalSourceAuditError = Get-BoundedText -Value $_.Exception.Message
            if ([string]::IsNullOrWhiteSpace($sourceAuditError)) { $sourceAuditError = $terminalSourceAuditError }
            [void]$finalFailures.Add("terminal source post-transaction audit failed: $terminalSourceAuditError")
        }
        if ($TestProbeSourceHandlesAfterTransaction) {
            try {
                Assert-LockedSourcesDenyWrite -LockedSources $sourceLocks
                $sourceHandlesHeldThroughTransaction = $true
            } catch {
                [void]$finalFailures.Add("source-handle transaction probe failed: $(Get-BoundedText -Value $_.Exception.Message)")
            }
        } else {
            $sourceHandlesHeldThroughTransaction = $true
        }
        try {
            Close-LockedSources -LockedSources $sourceLocks
            if ($TestInjectSourceHandleReleaseFailure) { throw "fixture injected source handle release failure" }
        } catch {
            $sourceHandleReleaseError = Get-BoundedText -Value $_.Exception.Message
            [void]$finalFailures.Add("source handle release failed: $sourceHandleReleaseError")
        }
        $sourceLocks = $null
    }

    $succeeded = $mainCompleted -and $finalFailures.Count -eq 0
    if ($succeeded) {
        $message = if ($WhatIf) { "verification passed; WhatIf suppressed canonical publication" } else { "verification passed and canonical report was atomically published" }
    } else {
        $message = Get-BoundedText -Value ([string]::Join("; ", $finalFailures)) -MaximumCharacters 8192
    }
}

if (-not $succeeded) {
    try {
        Send-Heartbeat -Stage "runner_failed" -ProcessId $workerProcessId -Elapsed $watch.Elapsed.TotalSeconds -Count $heartbeatCount -WorkerReport $workerReportPath -StdoutLog $stdoutLog -StderrLog $stderrLog -WorkerExitCode $workerExitCode
        $heartbeatCount++
    } catch {
        $message = Get-BoundedText -Value ($message + "; runner_failed heartbeat emission failed: " + $_.Exception.Message) -MaximumCharacters 8192
    }
}

$final = [ordered]@{
    schema = "oni.live-runtime-bootstrap-verification-run.v1"
    status = if ($succeeded) { "passed" } else { "failed" }
    action_id = "run-live-runtime-bootstrap-verification"
    startUtc = $startUtc.ToString("o")
    endUtc = (Get-Date).ToUniversalTime().ToString("o")
    durationSeconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    rounds = $Rounds
    whatIf = [bool]$WhatIf
    testFixtureMode = $testFixtureMode
    testRoot = if ($testFixtureMode) { $resolvedTestRoot } else { $null }
    heartbeatCount = $heartbeatCount
    workerProcessId = $workerProcessId
    workerExitCode = $workerExitCode
    workerReportPath = $workerReportPath
    workerAttestationPath = $workerAttestationPath
    canonicalReportPath = $canonicalPath
    reportPublished = $reportPublished
    message = $message
    executionRoot = $executionRoot
    validatedReport = $validatedSummary
    hashes = [ordered]@{
        sourcePre = $preSourceHashes
        sourcePost = $postSourceHashes
        canonicalPre = Get-SnapshotSummary -Snapshot $preCanonicalSnapshot
        canonicalPrePublish = Get-SnapshotSummary -Snapshot $prePublishCanonicalSnapshot
        canonicalPost = Get-SnapshotSummary -Snapshot $postCanonicalSnapshot
        candidateRawSha256 = $candidateRawSha256
        adjacentTempRawSha256 = $tempRawSha256
        publishedRawSha256 = $publishedRawSha256
        workerPycacheAttestationRawSha256 = $workerPycacheAttestationRawSha256
    }
    evidence = [ordered]@{
        threatModel = [ordered]@{
            concurrencyCoordination = "cooperative_publish_lock_plus_canonical_read_handle"
            postOperationIdentityChecks = $true
            handleBoundPathDeletion = $false
            activeNoncooperativeSameUserPathnameRace = "out_of_scope"
        }
        stdoutLog = $stdoutLog
        stderrLog = $stderrLog
        failureEvidencePreserved = (-not $succeeded -and -not [string]::IsNullOrWhiteSpace($executionRoot) -and (Test-Path -LiteralPath $executionRoot -PathType Container))
        executionRootOwnership = [ordered]@{
            tempRoot = $executionTempRoot
            tempRootIdentityBeforeCreate = $executionTempRootIdentityBefore
            tempRootIdentityAfterCreate = $executionTempRootIdentityAfter
            executionRootIdentity = $executionRootIdentity
            namePattern = '^r[0123456789abcdefghjkmnpqrstvwxyz]{25}[048cgmrw]$'
        }
        failedReport = $failedReportSummary
        killOnCloseJobAssigned = $jobAssigned
        jobActiveBeforeCleanup = $jobActiveBeforeCleanup
        jobActiveAfterCleanup = $jobActiveAfterCleanup
        jobCleanupSucceeded = $jobCleanupSucceeded
        copyDrainCompleted = $copyDrainCompleted
        cleanupError = Get-BoundedText -Value $cleanupError
        pycacheAttestation = [ordered]@{
            path = $workerAttestationPath
            rawLength = $workerPycacheAttestationRawLength
            rawSha256 = $workerPycacheAttestationRawSha256
            summary = $workerPycacheAttestation
        }
        resourceLimits = [ordered]@{
            deadlineSeconds = $TotalTimeoutSeconds
            monitorPollMaximumMilliseconds = 1000
            stdoutHardCaptureLimitBytes = $script:MaximumStdoutBytes
            stderrHardCaptureLimitBytes = $script:MaximumStderrBytes
            reportSnapshotReadLimitBytes = $script:MaximumSnapshotBytes
            reportObservedKillThresholdBytes = $script:MaximumSnapshotBytes
            executionRootObservedKillThresholdBytes = $script:MaximumExecutionRootBytes
            executionRootEntryKillThreshold = $script:MaximumExecutionRootEntries
            stdoutEnforcement = "hard_capped_write_stream_then_job_kill"
            stderrEnforcement = "hard_capped_write_stream_then_job_kill"
            reportEnforcement = "observed_poll_then_job_kill_and_bounded_snapshot_read"
            executionRootEnforcement = "observed_lazy_enumeration_then_job_kill"
            stdoutBytesObserved = $stdoutBytesObserved
            stderrBytesObserved = $stderrBytesObserved
            reportBytesObserved = $reportBytesObserved
            executionRootBytesObserved = $executionRootBytesObserved
            executionRootFilesObserved = $executionRootFilesObserved
            executionRootDirectoriesObserved = $executionRootDirectoriesObserved
            executionRootEntriesObserved = $executionRootEntriesObserved
            executionRootScanAttemptLimit = $script:MaximumExecutionRootScanAttempts
            executionRootScanRestartsObserved = $executionRootScanRestartsObserved
            executionRootTransientDisappearancesObserved = $executionRootTransientDisappearancesObserved
            fixtureNestedRenameInjected = [bool]$script:TestResourceMonitorNestedRenameInjected
            fixtureEnumerationIoFailureInjected = [bool]$script:TestResourceMonitorEnumerationIoFailureInjected
            fixtureTransientChurnInjected = [bool]$script:TestResourceMonitorTransientChurnInjected
            fixturePersistentChurnInjectionCount = [long]$script:TestResourceMonitorPersistentChurnInjectionCount
            stdoutTruncated = $stdoutLogTruncated
            stderrTruncated = $stderrLogTruncated
            violation = Get-BoundedText -Value $resourceLimitError
        }
        postAudits = [ordered]@{
            sourceObserved = $sourceAuditObserved
            sourceError = $sourceAuditError
            canonicalObserved = Get-SnapshotSummary -Snapshot $canonicalAuditObserved
            canonicalError = $canonicalAuditError
        }
        publishLockReleaseError = $publishLockReleaseError
        publishLockReleased = $publishLockReleased
        publishLockCleanupError = $publishLockCleanupError
        publishedCanonicalBridgeHeld = $publishedCanonicalBridgeHeld
        publishedCanonicalBridgeError = $publishedCanonicalBridgeError
        publicationTransactionCompleted = $publicationTransactionCompleted
        sourceHandlesHeldThroughTransaction = $sourceHandlesHeldThroughTransaction
        sourceHandleReleaseError = $sourceHandleReleaseError
        rollback = $rollbackEvidence
    }
}

[Console]::Out.WriteLine(($final | ConvertTo-Json -Depth 12 -Compress))
exit $(if ($succeeded) { 0 } else { 1 })
