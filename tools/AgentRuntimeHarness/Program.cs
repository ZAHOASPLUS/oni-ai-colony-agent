using System;
using System.Threading.Tasks;

namespace OniAiProbeMod.Agent.Harness
{
    internal static class Program
    {
        private static async Task<int> Main()
        {
            try
            {
                await RuntimeTests.RunAll();
                await OpenAiClientTests.RunAll();
                RollbackBudgetTests.RunAll();
                SchemaTests.Run();
                Console.WriteLine("PASS: isolated runtime, context, transport, ledger and schema harness.");
                Console.WriteLine("This does not validate Unity hooks, game handlers, real providers, new-game APIs or native autosaves.");
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("FAIL: " + error);
                return 1;
            }
        }
    }
}
