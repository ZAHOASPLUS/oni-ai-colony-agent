using System;
using System.IO;
using System.Text;

namespace OniAiProbeMod.Agent
{
    internal static class AgentKnowledge
    {
        /// <summary>Load a bounded phase pack from the existing repository knowledge.</summary>
        internal static string ForCycle(int cycle)
        {
            var text = new StringBuilder("Gameplay reference only; current player instructions and configured permissions override these notes.\n");
            string[] files = cycle <= 30 ? new[] { "opening", "survival", "layouts" } : new[] { "survival", "resources", "traps" };
            foreach (string file in files)
            {
                using (Stream stream = typeof(AgentKnowledge).Assembly.GetManifestResourceStream("OniAiProbeMod.knowledge." + file + ".md"))
                {
                    if (stream == null) throw new InvalidOperationException("Embedded gameplay knowledge is missing.");
                    using (StreamReader reader = new StreamReader(stream)) text.AppendLine(reader.ReadToEnd());
                }
            }
            if (text.Length > 24000)
                return text.ToString(0, 23800) + "\n[Knowledge pack explicitly truncated to the configured bounded reference size.]";
            return text.ToString();
        }
    }
}
