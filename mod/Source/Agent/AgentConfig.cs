using System;
using System.Net;
using Newtonsoft.Json;

namespace OniAiProbeMod.Agent
{
    /// <summary>Ordinary settings only. Credentials belong exclusively to CredentialStore.</summary>
    [JsonObject(MemberSerialization.OptIn)]
    public sealed class AgentConfig
    {
        [JsonProperty] public bool Enabled { get; set; }
        [JsonProperty] public string BaseUrl { get; set; } = "";
        [JsonProperty] public string Model { get; set; } = "";
        [JsonProperty] public bool AllowLocalhostHttp { get; set; }
        [JsonProperty] public int RequestTimeoutSeconds { get; set; } = 180;
        [JsonProperty] public int MaxRequestBytes { get; set; } = 2 * 1024 * 1024;
        [JsonProperty] public int MaxResponseBytes { get; set; } = 1024 * 1024;
        [JsonProperty] public int MaxRetries { get; set; } = 2;
        [JsonProperty] public int MaxRetryDelaySeconds { get; set; } = 8;
        [JsonProperty] public int MaxCompletionTokens { get; set; } = 16384;
        [JsonProperty] public int MaxToolCallsPerTurn { get; set; } = 16;
        [JsonProperty] public int MaxModelTurns { get; set; } = 400;
        [JsonProperty] public int MaxToolCallsTotal { get; set; } = 2000;
        [JsonProperty] public int MaxSessionMinutes { get; set; } = 240;
        /// <summary>Keep playing after a text-only reply, checking in every ContinueDelaySeconds.</summary>
        [JsonProperty] public bool Continuous { get; set; } = true;
        [JsonProperty] public int ContinueDelaySeconds { get; set; } = 30;
        [JsonProperty] public int MaxContextMessages { get; set; } = 256;
        [JsonProperty] public int MaxContextCharacters { get; set; } = 400000;
        [JsonProperty] public int MaxResultCharacters { get; set; } = 32000;
        [JsonProperty] public string Goal { get; set; } = "自主经营殖民地，优先保障氧气、食物、水和电力，逐步建立可持续生存体系。";
        [JsonProperty] public string PausePolicy { get; set; } = "never_pause";
        [JsonProperty] public bool RollbackEnabled { get; set; }
        [JsonProperty] public int RollbackMaxPerColony { get; set; } = 3;

        public AgentConfig Snapshot()
        {
            // All settings are immutable value types or strings; never copy a credential here.
            return (AgentConfig)MemberwiseClone();
        }

        public void Validate(bool requireConnection = false)
        {
            CheckRange(RequestTimeoutSeconds, 1, 300, nameof(RequestTimeoutSeconds));
            CheckRange(MaxRequestBytes, 1024, 4 * 1024 * 1024, nameof(MaxRequestBytes));
            CheckRange(MaxResponseBytes, 1024, 8 * 1024 * 1024, nameof(MaxResponseBytes));
            CheckRange(MaxRetries, 0, 5, nameof(MaxRetries));
            CheckRange(MaxRetryDelaySeconds, 1, 60, nameof(MaxRetryDelaySeconds));
            CheckRange(MaxCompletionTokens, 1, 128000, nameof(MaxCompletionTokens));
            CheckRange(MaxToolCallsPerTurn, 1, 32, nameof(MaxToolCallsPerTurn));
            CheckRange(MaxModelTurns, 1, 100000, nameof(MaxModelTurns));
            CheckRange(MaxToolCallsTotal, 1, 1000000, nameof(MaxToolCallsTotal));
            CheckRange(MaxSessionMinutes, 1, 7 * 24 * 60, nameof(MaxSessionMinutes));
            CheckRange(ContinueDelaySeconds, 1, 3600, nameof(ContinueDelaySeconds));
            CheckRange(MaxContextMessages, 4, 512, nameof(MaxContextMessages));
            CheckRange(MaxContextCharacters, 2048, 1024 * 1024, nameof(MaxContextCharacters));
            CheckRange(MaxResultCharacters, 512, 128 * 1024, nameof(MaxResultCharacters));
            CheckRange(RollbackMaxPerColony, 0, 100, nameof(RollbackMaxPerColony));
            if (MaxContextMessages < MaxToolCallsPerTurn + 3)
                throw new ArgumentException("Context must fit the system, instruction and a complete tool exchange.");
            if (string.IsNullOrWhiteSpace(Goal) || Goal.Length > 16000)
                throw new ArgumentException("Goal must contain 1 to 16000 characters.");
            if (PausePolicy != "agent_decides" && PausePolicy != "never_pause" && PausePolicy != "allow_pause_require_resume")
                throw new ArgumentException("Unknown pause policy.");
            if (!string.IsNullOrEmpty(BaseUrl) || Enabled || requireConnection)
                GetChatCompletionsEndpoint();
            if (Enabled || requireConnection || !string.IsNullOrEmpty(Model))
            {
                if (string.IsNullOrWhiteSpace(Model) || Model.Length > 256 || Model != Model.Trim() || HasControl(Model))
                    throw new ArgumentException("A model ID of 1 to 256 characters is required.");
            }
        }

        /// <summary>Retain the explicit path; never add a provider-specific /v1 prefix.</summary>
        public Uri GetChatCompletionsEndpoint()
        {
            if (string.IsNullOrWhiteSpace(BaseUrl) || BaseUrl.Length > 2048 || HasControl(BaseUrl))
                throw new ArgumentException("An explicit model service Base URL is required.");
            string text = BaseUrl.Trim();
            // Backslashes and encoded controls are ambiguous between URI and HTTP parsers.
            if (text.IndexOf('\\') >= 0 || HasEncodedControl(text))
                throw new ArgumentException("The Base URL contains unsupported characters.");
            Uri uri;
            if (!Uri.TryCreate(text, UriKind.Absolute, out uri) || string.IsNullOrEmpty(uri.Host) ||
                !string.IsNullOrEmpty(uri.UserInfo) || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
                throw new ArgumentException("The Base URL must be absolute with no credentials, query or fragment.");
            bool local = IsLoopbackHost(uri);
            if (uri.Scheme != Uri.UriSchemeHttps &&
                !(uri.Scheme == Uri.UriSchemeHttp && AllowLocalhostHttp && local))
                throw new ArgumentException("HTTPS is required; loopback HTTP needs explicit opt-in.");
            string path = uri.AbsolutePath.TrimEnd('/');
            if (!path.EndsWith("/chat/completions", StringComparison.Ordinal))
                path += "/chat/completions";
            UriBuilder normalized = new UriBuilder(uri) { Path = path };
            return normalized.Uri;
        }

        internal static bool IsLoopbackHost(Uri uri)
        {
            if (string.Equals(uri.DnsSafeHost, "localhost", StringComparison.OrdinalIgnoreCase))
                return true;
            IPAddress address;
            return IPAddress.TryParse(uri.DnsSafeHost.Trim('[', ']'), out address) &&
                (IPAddress.IsLoopback(address) || (address.IsIPv4MappedToIPv6 && IPAddress.IsLoopback(address.MapToIPv4())));
        }

        private static bool HasControl(string value)
        {
            foreach (char character in value)
                if (char.IsControl(character)) return true;
            return false;
        }

        private static bool HasEncodedControl(string value)
        {
            // URL decoding is used for validation only, never for endpoint reconstruction.
            try { return HasControl(Uri.UnescapeDataString(value)); }
            catch (UriFormatException) { return true; }
        }

        private static void CheckRange(int value, int minimum, int maximum, string setting)
        {
            if (value < minimum || value > maximum)
                throw new ArgumentOutOfRangeException(setting, "Setting is outside its supported range.");
        }
    }
}
