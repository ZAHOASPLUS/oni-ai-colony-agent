using System;
using System.IO;
using System.Text;

namespace OniAiProbeMod.Agent
{
    internal static class AgentKnowledge
    {
        internal const int MaxCharacters = 120000;

        /// <summary>
        /// The play skill plus every knowledge file, the current phase's files first.
        /// Whole files only: a file that does not fit is left out and named, never cut
        /// in half.  (The first version left SKILL.md out, dropped the opening and
        /// layout notes after cycle 30, and cut text at 24000 characters.)
        /// </summary>
        internal static string ForCycle(int cycle)
        {
            var text = new StringBuilder("Gameplay reference only; current player instructions and configured permissions override these notes.\n");
            string[] files = cycle <= 30
                ? new[] { "SKILL", "opening", "survival", "layouts", "resources", "traps" }
                : new[] { "SKILL", "survival", "resources", "traps", "layouts", "opening" };
            var skipped = new StringBuilder();
            foreach (string file in files)
            {
                using (Stream stream = typeof(AgentKnowledge).Assembly.GetManifestResourceStream("OniAiProbeMod.knowledge." + file + ".md"))
                {
                    if (stream == null) throw new InvalidOperationException("Embedded gameplay knowledge is missing: " + file);
                    string body;
                    using (StreamReader reader = new StreamReader(stream)) body = reader.ReadToEnd();
                    if (text.Length + body.Length + 64 > MaxCharacters) { skipped.Append(' ').Append(file); continue; }
                    text.Append("\n## ").Append(file).Append(".md\n").AppendLine(body);
                }
            }
            if (skipped.Length > 0) text.Append("\n[Left out for size:").Append(skipped).Append("]\n");
            return text.ToString();
        }
    }
}
