using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

namespace OniAiProbeMod.Agent
{
    public sealed class CredentialStoreException : Exception
    {
        public string Code { get; }

        internal CredentialStoreException(string code)
            : base("Credential operation failed (" + code + "). No plaintext fallback is available.")
        {
            Code = code;
        }
    }

    /// <summary>
    /// DPAPI CurrentUser storage, separate from normal config and saves. Unsupported
    /// platforms fail closed. This class never logs credentials or inner exceptions.
    /// </summary>
    public sealed class CredentialStore
    {
        private const int MaximumCipherBytes = 65536;
        private static readonly byte[] Magic = Encoding.ASCII.GetBytes("ONIAGENTKEY1\n");
        private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("OniAiProbeMod.Agent.CredentialStore.v1");
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private readonly string directory;
        private readonly string path;
        private readonly object sync = new object();

        public CredentialStore(string directory)
        {
            if (string.IsNullOrWhiteSpace(directory)) throw new ArgumentException("Credential directory is required.");
            this.directory = Path.GetFullPath(directory);
            path = Path.Combine(this.directory, "agent-key.dpapi");
        }

        public static bool IsSupported
        {
            get
            {
                if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows)) return false;
                try
                {
                    Type scope;
                    Type data = FindDpapi(out scope);
                    return data != null && scope != null &&
                        data.GetMethod("Protect", new[] { typeof(byte[]), typeof(byte[]), scope }) != null &&
                        data.GetMethod("Unprotect", new[] { typeof(byte[]), typeof(byte[]), scope }) != null;
                }
                catch { return false; }
            }
        }

        public void Save(string apiKey)
        {
            RequireSupported();
            ValidateApiKey(apiKey);
            byte[] plaintext = StrictUtf8.GetBytes(apiKey);
            string temporary = null;
            try
            {
                byte[] ciphertext = Transform("Protect", plaintext);
                if (ciphertext == null || ciphertext.Length > MaximumCipherBytes)
                    throw new CredentialStoreException("invalid_ciphertext");
                lock (sync)
                {
                    Directory.CreateDirectory(directory);
                    RejectReparsePoints();
                    temporary = Path.Combine(directory, "agent-key." + Guid.NewGuid().ToString("N") + ".tmp");
                    using (FileStream output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        output.Write(Magic, 0, Magic.Length);
                        output.Write(ciphertext, 0, ciphertext.Length);
                        output.Flush(true);
                    }
                    // The previous encrypted file survives an unsuccessful replace.
                    if (File.Exists(path)) File.Replace(temporary, path, null);
                    else File.Move(temporary, path);
                    temporary = null;
                }
            }
            catch (CredentialStoreException) { throw; }
            catch { throw new CredentialStoreException("write_failed"); }
            finally
            {
                Array.Clear(plaintext, 0, plaintext.Length);
                if (temporary != null)
                {
                    try { File.Delete(temporary); }
                    catch { /* Only encrypted bytes can remain here. */ }
                }
            }
        }

        /// <returns>The key, or null if none has been saved. Errors never become a plaintext fallback.</returns>
        public string Load()
        {
            RequireSupported();
            byte[] plaintext = null;
            try
            {
                byte[] ciphertext;
                lock (sync)
                {
                    RejectReparsePoints();
                    if (!File.Exists(path)) return null;
                    using (FileStream input = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                    {
                        if (input.Length <= Magic.Length || input.Length > MaximumCipherBytes + Magic.Length)
                            throw new CredentialStoreException("invalid_file");
                        for (int index = 0; index < Magic.Length; index++)
                            if (input.ReadByte() != Magic[index]) throw new CredentialStoreException("invalid_file");
                        ciphertext = new byte[(int)input.Length - Magic.Length];
                        int read = 0;
                        while (read < ciphertext.Length)
                        {
                            int count = input.Read(ciphertext, read, ciphertext.Length - read);
                            if (count == 0) throw new CredentialStoreException("invalid_file");
                            read += count;
                        }
                    }
                }
                plaintext = Transform("Unprotect", ciphertext);
                if (plaintext == null || plaintext.Length == 0 || plaintext.Length > 4096)
                    throw new CredentialStoreException("invalid_key");
                string key = StrictUtf8.GetString(plaintext);
                ValidateApiKey(key);
                return key;
            }
            catch (CredentialStoreException) { throw; }
            catch { throw new CredentialStoreException("read_failed"); }
            finally
            {
                if (plaintext != null) Array.Clear(plaintext, 0, plaintext.Length);
            }
        }

        public void Delete()
        {
            RequireSupported();
            try
            {
                lock (sync)
                {
                    RejectReparsePoints();
                    File.Delete(path);
                }
            }
            catch (CredentialStoreException) { throw; }
            catch { throw new CredentialStoreException("delete_failed"); }
        }

        internal static void ValidateApiKey(string apiKey)
        {
            if (string.IsNullOrEmpty(apiKey) || apiKey.Length > 4096)
                throw new ArgumentException("An API key of 1 to 4096 visible ASCII characters is required.");
            foreach (char character in apiKey)
                if (character < 33 || character > 126)
                    throw new ArgumentException("The API key contains unsupported characters.");
        }

        private static void RequireSupported()
        {
            if (!IsSupported) throw new CredentialStoreException("dpapi_unavailable");
        }

        private static byte[] Transform(string operation, byte[] value)
        {
            try
            {
                Type scope;
                Type data = FindDpapi(out scope);
                if (data == null || scope == null) throw new CredentialStoreException("dpapi_unavailable");
                MethodInfo method = data.GetMethod(operation, new[] { typeof(byte[]), typeof(byte[]), scope });
                if (method == null) throw new CredentialStoreException("dpapi_unavailable");
                object currentUser = Enum.Parse(scope, "CurrentUser");
                return (byte[])method.Invoke(null, new object[] { value, Entropy, currentUser });
            }
            catch (CredentialStoreException) { throw; }
            catch { throw new CredentialStoreException("dpapi_failed"); }
        }

        private static Type FindDpapi(out Type scope)
        {
            // Mono/ONI may expose DPAPI in System.Security rather than the .NET assembly.
            // Reflection avoids adding a package dependency to the netstandard game mod.
            Type data = Type.GetType("System.Security.Cryptography.ProtectedData, System.Security.Cryptography.ProtectedData", false)
                ?? Type.GetType("System.Security.Cryptography.ProtectedData, System.Security", false);
            scope = data == null ? null : data.Assembly.GetType("System.Security.Cryptography.DataProtectionScope", false);
            return data;
        }

        private void RejectReparsePoints()
        {
            for (DirectoryInfo current = new DirectoryInfo(directory); current != null; current = current.Parent)
            {
                if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new CredentialStoreException("unsafe_path");
            }
            if (File.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new CredentialStoreException("unsafe_path");
        }
    }
}
