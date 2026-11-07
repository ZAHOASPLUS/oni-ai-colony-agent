using System;

namespace OniAiProbeMod.Agent
{
    /// <summary>Process-local single writer. Checked on the main thread at execution.</summary>
    internal static class AgentWriteLease
    {
        private static readonly object Sync = new object();
        private static string session;
        internal static bool Acquire(string sessionId)
        {
            if (string.IsNullOrEmpty(sessionId)) return false;
            lock (Sync)
            {
                if (session != null && session != sessionId) return false;
                session = sessionId;
                return true;
            }
        }
        internal static void Release(string sessionId)
        {
            lock (Sync) if (session == sessionId) session = null;
        }
        internal static bool Allows(string owner, string sessionId = null)
        {
            lock (Sync)
                return owner == "agent" ? session != null && session == sessionId : session == null;
        }
    }
}
