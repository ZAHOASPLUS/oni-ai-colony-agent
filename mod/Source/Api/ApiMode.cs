using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The tool API exists only for a process that was launched for it: the
    /// launcher writes ai-bridge/autoload.json right before starting the game.
    /// The file is consumed here, so a crash cannot loop, and an old one is
    /// ignored, so normal play is never changed by a launch that did not happen.
    /// </summary>
    internal static class ApiMode
    {
        internal const string AutoloadFileName = "autoload.json";
        private const double AutoloadMaxAgeSeconds = 300.0;

        internal static bool Enabled { get; private set; }

        /// <summary>The save to load at the main menu, if any.  Cleared by <see cref="TakePendingSave"/>.</summary>
        internal static string PendingSave { get; private set; }

        internal static void Initialize(string bridgeDirectory)
        {
            string path = Path.Combine(bridgeDirectory, AutoloadFileName);
            if (!File.Exists(path))
            {
                return;
            }

            string refusal = null;
            try
            {
                JObject request = ApiJson.Parse(File.ReadAllText(path));
                DateTimeOffset created = DateTimeOffset.Parse(
                    (string)request["created_utc"],
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal);
                double age = (DateTimeOffset.UtcNow - created).TotalSeconds;
                if (age > AutoloadMaxAgeSeconds || age < -AutoloadMaxAgeSeconds)
                {
                    refusal = "stale_autoload";
                }
                else
                {
                    Enabled = true;
                    PendingSave = (string)request["save"];
                }
            }
            catch (Exception ex) when (ex is IOException || ex is JsonException || ex is FormatException ||
                ex is ArgumentException || ex is InvalidCastException || ex is UnauthorizedAccessException)
            {
                refusal = "unreadable_autoload";
            }

            try
            {
                File.Delete(path);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                // Harmless: it ages out.
                Debug.LogWarning(Mod.LogPrefix + " Could not delete " + AutoloadFileName + ": " + ex.Message);
            }

            ApiLog.Event("api_mode", new Dictionary<string, object>
            {
                { "enabled", Enabled },
                { "refusal", refusal },
                { "autoload", !string.IsNullOrEmpty(PendingSave) }
            });
            Debug.Log(Mod.LogPrefix + " API mode " + (Enabled ? "enabled" : "refused: " + refusal));
        }

        internal static string TakePendingSave()
        {
            string save = PendingSave;
            PendingSave = null;
            return save;
        }
    }

    /// <summary>
    /// Refuses anything but a named test clone.  The launcher verifies the
    /// clone's hashes against its manifest before writing autoload.json; this is
    /// the in-game backstop, and the only check load_save has.
    /// </summary>
    internal static class ApiSaveGuard
    {
        internal const string ClonePrefix = "oni-";

        /// <summary>Null when the save may be loaded, otherwise the reason it may not.</summary>
        internal static string Refusal(string path)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return "No save path was given.";
            }

            string full;
            try
            {
                full = Path.GetFullPath(path);
            }
            catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is PathTooLongException)
            {
                return "The save path is invalid.";
            }

            if (!string.Equals(Path.GetExtension(full), ".sav", StringComparison.OrdinalIgnoreCase))
            {
                return "Not a .sav file.";
            }
            string root = Path.GetFullPath(Util.RootFolder()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                return "The save is outside the game's save folder.";
            }

            if (!IsClone(full))
            {
                return "Only a test clone may be loaded: the file and its folder must both start with \"" +
                    ClonePrefix + "\".";
            }
            if (!File.Exists(full))
            {
                return "The save is missing.";
            }
            // A checkpoint's identity is its manifest (decision D4), not a preview.
            bool checkpoint = Path.GetFileNameWithoutExtension(full).Contains("-ckpt-");
            if (!checkpoint && !File.Exists(Path.ChangeExtension(full, ".png")))
            {
                return "The save's preview .png is missing.";
            }
            return null;
        }

        internal static bool IsClone(string path)
        {
            if (string.IsNullOrEmpty(path))
            {
                return false;
            }
            string stem = Path.GetFileNameWithoutExtension(path);
            string folder = Path.GetFileName(Path.GetDirectoryName(path));
            return stem.StartsWith(ClonePrefix, StringComparison.Ordinal) &&
                folder.StartsWith(ClonePrefix, StringComparison.Ordinal);
        }
    }
}
