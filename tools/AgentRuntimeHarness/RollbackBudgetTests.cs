using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;

namespace OniAiProbeMod.Agent.Harness
{
    // Filesystem-only tests. The fake below is deliberately NOT a native ONI autosave adapter.
    internal static class RollbackBudgetTests
    {
        public static void RunAll()
        {
            MissingStoresNeverInitializeImplicitly();
            EnrollmentRequiresVerifiedCreation();
            DebitPrecedesReceiptAndSurvivesRestart();
            FailureNeverRefunds();
            DuplicateRequestsNeverAuthorizeAnotherLoad();
            ColonyBudgetsAreIndependent();
            RejectUnverifiedTargetsAndPaths();
            MissingAndCorruptFilesDisableRollback();
            RestoredOldLedgerFailsWatermarkCheck();
            WriteFailureReturnsNoReceipt();
            CustodyRejectsConcurrentAccess();
            ConcurrentInstancesCannotOverspend();
            ConfigChangesDoNotResetConsumption();
            OutcomeUpdatesAreIdempotentButCannotRewriteHistory();
            VerifierFailureAndInvalidCyclesFailClosed();
            Console.WriteLine("PASS: 15 rollback ledger scenarios");
        }

        private static void MissingStoresNeverInitializeImplicitly()
        {
            using (var f = new Fixture(false))
            {
                ExpectCode(f.Ledger.ReadBudget("colony_a"), "rollback_ledger_missing");
                ExpectCode(f.Reserve("r1"), "rollback_ledger_missing");
                Expect(!Directory.Exists(f.Root), "Reads/reservation must not create a store.");
                Expect(f.Ledger.ProvisionNewStore().Success, "Explicit provisioning failed.");
                ExpectCode(f.Ledger.ProvisionNewStore(), "rollback_store_already_exists");
                ExpectCode(f.Reserve("r1"), "rollback_colony_not_registered");
            }
        }

        private static void EnrollmentRequiresVerifiedCreation()
        {
            using (var f = new Fixture(false))
            {
                Expect(f.Ledger.ProvisionNewStore().Success, "Provision failed.");
                f.Verifier.IsNewColony = false;
                ExpectCode(f.Ledger.TryRegisterNewColony("colony_a", "session_a"), "rollback_identity_unverified");
                f.Verifier.IsNewColony = true;
                Expect(f.Ledger.TryRegisterNewColony("colony_a", "session_a").Success, "New colony was rejected.");
                Expect(f.Reserve("r1").LoadAuthorized, "First reserve failed.");
                ExpectCode(f.Ledger.TryRegisterNewColony("colony_a", "session_a"), "rollback_colony_already_registered");
                Expect(f.Ledger.ReadBudget("colony_a").Consumed == 1, "Re-enrollment reset the budget.");
            }
            using (var f = new Fixture(false))
            {
                Expect(f.Ledger.ProvisionNewStore().Success, "Provision failed.");
                var unavailable = new RollbackBudgetLedger(f.Root, null);
                ExpectCode(unavailable.TryRegisterNewColony("colony_a", "session_a"), "l3_unavailable");
            }
        }

        private static void DebitPrecedesReceiptAndSurvivesRestart()
        {
            using (var f = new Fixture())
            {
                var reservation = f.Reserve("r1");
                Expect(reservation.Success && reservation.LoadAuthorized && reservation.Consumed == 1, "No debit receipt.");
                Expect(reservation.Attempt.Outcome == "pending", "Initial record must be pending.");
                var restarted = new RollbackBudgetLedger(f.Root, f.Verifier);
                var snapshot = restarted.ReadBudget("colony_a");
                Expect(snapshot.Success && snapshot.Consumed == 1 && !snapshot.LoadAuthorized, "Restart reset or reauthorized debit.");
                Expect(snapshot.Attempt.AttemptId == reservation.Attempt.AttemptId && snapshot.Attempt.Outcome == "pending", "Pending history was lost.");
                Expect(snapshot.Attempt.SessionId == "session_a" && snapshot.Attempt.AutosaveId == "native_slot_1", "Attempt scope lost.");
                Expect(snapshot.Attempt.Cycle == 42.5 && snapshot.Attempt.Reason == "test reason", "Attempt metadata lost.");
                ExpectCode(restarted.TryReserve("colony_a", "session_a", "native_slot_1", "r2", 1, "test"), "rollback_limit_exhausted");
            }
        }

        private static void FailureNeverRefunds()
        {
            using (var f = new Fixture())
            {
                var reservation = f.Reserve("r1");
                var outcome = f.Ledger.MarkOutcome("colony_a", reservation.Attempt.AttemptId, false, "load_failed");
                Expect(outcome.Success && !outcome.LoadAuthorized && outcome.Consumed == 1, "Failed load refunded or reauthorized.");
                var restarted = new RollbackBudgetLedger(f.Root, f.Verifier);
                Expect(restarted.ReadBudget("colony_a").Attempt.Outcome == "failed", "Failure was not durable.");
                ExpectCode(restarted.TryReserve("colony_a", "session_a", "native_slot_1", "r2", 1, "test"), "rollback_limit_exhausted");
            }
        }

        private static void DuplicateRequestsNeverAuthorizeAnotherLoad()
        {
            using (var f = new Fixture())
            {
                var original = f.Reserve("r1");
                Expect(original.LoadAuthorized, "First request was not authorized.");
                ExpectCode(f.Reserve("r1"), "rollback_attempt_already_recorded");
                Expect(f.Ledger.MarkOutcome("colony_a", original.Attempt.AttemptId, true, "loaded").Success, "Outcome failed.");
                f.Verifier.SessionId = "session_b";
                var restarted = new RollbackBudgetLedger(f.Root, f.Verifier);
                ExpectCode(restarted.TryReserve("colony_a", "session_b", "native_slot_1", "r1", 3, "retry"), "rollback_attempt_already_recorded");
                Expect(restarted.ReadBudget("colony_a").Consumed == 1, "Duplicate request consumed again.");
            }
        }

        private static void ColonyBudgetsAreIndependent()
        {
            using (var f = new Fixture())
            {
                Expect(f.Reserve("r1").LoadAuthorized, "First colony reserve failed.");
                f.Verifier.ColonyId = "colony_b";
                f.Verifier.SessionId = "session_b";
                Expect(f.Ledger.TryRegisterNewColony("colony_b", "session_b").Success, "Second enrollment failed.");
                var second = f.Ledger.TryReserve("colony_b", "session_b", "native_slot_1", "r1", 1, "test");
                Expect(second.LoadAuthorized && second.Consumed == 1, "Different colony shared a budget.");
                Expect(f.Ledger.ReadBudget("colony_a").Consumed == 1, "First colony history changed.");
                ExpectCode(f.Ledger.TryReserve("colony_a", "session_b", "native_slot_1", "r2", 3, "test"), "rollback_identity_unverified");
            }
        }

        private static void RejectUnverifiedTargetsAndPaths()
        {
            using (var f = new Fixture())
            {
                foreach (string target in new[] { "../manual.sav", "C:\\save.sav", "/tmp/save.sav", "https://save", "", "two words" })
                    ExpectCode(f.Ledger.TryReserve("colony_a", "session_a", target, "r1", 3, "test"), "invalid_rollback_identity");
                ExpectCode(f.Ledger.TryReserve("colony_a", "session_a", "manual_save", "r1", 3, "test"), "rollback_target_unverified");
                ExpectCode(f.Ledger.TryReserve("colony_a", "stale_session", "native_slot_1", "r1", 3, "test"), "rollback_identity_unverified");
                f.Verifier.HasAutosave = false;
                ExpectCode(f.Reserve("r1"), "no_autosave_available");
                Expect(f.Ledger.ReadBudget("colony_a").Consumed == 0, "Invalid target consumed budget.");
                var unavailable = new RollbackBudgetLedger(f.Root, null);
                ExpectCode(unavailable.TryReserve("colony_a", "session_a", "native_slot_1", "r1", 3, "test"), "l3_unavailable");
            }
        }

        private static void MissingAndCorruptFilesDisableRollback()
        {
            foreach (string file in new[] { RollbackBudgetLedger.LedgerFileName, RollbackBudgetLedger.CustodyFileName })
            {
                using (var f = new Fixture())
                {
                    Expect(f.Reserve("r1").LoadAuthorized, "Reserve failed.");
                    File.Delete(Path.Combine(f.Root, file));
                    ExpectCode(f.Reserve("r2"), "rollback_ledger_missing");
                    ExpectCode(f.Ledger.ProvisionNewStore(), "rollback_store_already_exists");
                }
                using (var f = new Fixture())
                {
                    File.WriteAllText(Path.Combine(f.Root, file), "{}");
                    ExpectCode(f.Reserve("r1"), "rollback_ledger_corrupt");
                    ExpectCode(f.Ledger.ProvisionNewStore(), "rollback_store_already_exists");
                }
            }
            using (var f = new Fixture())
            {
                string path = Path.Combine(f.Root, RollbackBudgetLedger.LedgerFileName);
                string body = File.ReadAllText(path);
                File.WriteAllText(path, body.Replace("colony_a", "colony_b")); // Still valid JSON, invalid integrity hash.
                ExpectCode(f.Reserve("r1"), "rollback_ledger_corrupt");
            }
        }

        private static void RestoredOldLedgerFailsWatermarkCheck()
        {
            using (var f = new Fixture())
            {
                string path = Path.Combine(f.Root, RollbackBudgetLedger.LedgerFileName);
                byte[] oldLedger = File.ReadAllBytes(path);
                Expect(f.Reserve("r1").LoadAuthorized, "Reserve failed.");
                File.WriteAllBytes(path, oldLedger);
                ExpectCode(new RollbackBudgetLedger(f.Root, f.Verifier).ReadBudget("colony_a"), "rollback_ledger_revision_mismatch");
                ExpectCode(f.Reserve("r2"), "rollback_ledger_revision_mismatch");
            }
        }

        private static void WriteFailureReturnsNoReceipt()
        {
            using (var f = new Fixture())
            {
                string path = Path.Combine(f.Root, RollbackBudgetLedger.LedgerFileName);
                byte[] previous = File.ReadAllBytes(path);
                // Portable fault injection after read/verification but before atomic replacement.
                f.Verifier.OnVerify = () => { File.Delete(path); Directory.CreateDirectory(path); };
                var attempt = f.Reserve("r1");
                Expect(!attempt.Success && !attempt.LoadAuthorized && attempt.Attempt == null, "Failed write returned a load receipt.");
                f.Verifier.OnVerify = null;
                Directory.Delete(path);
                File.WriteAllBytes(path, previous);
                ExpectCode(f.Reserve("r2"), "rollback_ledger_revision_mismatch");
                ExpectCode(f.Ledger.ProvisionNewStore(), "rollback_store_already_exists");
            }
        }

        private static void CustodyRejectsConcurrentAccess()
        {
            using (var f = new Fixture())
            {
                using (new FileStream(Path.Combine(f.Root, RollbackBudgetLedger.CustodyFileName), FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    var result = f.Reserve("r1");
                    Expect(!result.Success && !result.LoadAuthorized, "Exclusive custody was bypassed.");
                }
                Expect(f.Reserve("r1").LoadAuthorized, "Custody was not released.");
            }
        }

        private static void ConcurrentInstancesCannotOverspend()
        {
            using (var f = new Fixture())
            {
                var tasks = Enumerable.Range(0, 16).Select(i => Task.Run(() =>
                    new RollbackBudgetLedger(f.Root, f.Verifier).TryReserve("colony_a", "session_a", "native_slot_1", "request_" + i, 1, "test"))).ToArray();
                Task.WaitAll(tasks);
                Expect(tasks.Count(t => t.Result.LoadAuthorized) == 1, "Concurrent instances overspent or no request succeeded.");
                Expect(f.Ledger.ReadBudget("colony_a").Consumed == 1, "Concurrent history is inconsistent.");
            }
        }

        private static void ConfigChangesDoNotResetConsumption()
        {
            using (var f = new Fixture())
            {
                ExpectCode(f.Ledger.TryReserve("colony_a", "session_a", "native_slot_1", "r1", 0, "test"), "rollback_limit_exhausted");
                Expect(f.Reserve("r1").LoadAuthorized, "Reserve failed.");
                ExpectCode(f.Ledger.TryReserve("colony_a", "session_a", "native_slot_1", "r2", 1, "test"), "rollback_limit_exhausted");
                Expect(f.Ledger.TryReserve("colony_a", "session_a", "native_slot_1", "r2", 2, "test").Consumed == 2, "Raising player limit reset accounting.");
                ExpectCode(f.Ledger.TryReserve("colony_a", "session_a", "native_slot_1", "r3", -1, "test"), "invalid_rollback_request");
            }
        }

        private static void OutcomeUpdatesAreIdempotentButCannotRewriteHistory()
        {
            using (var f = new Fixture())
            {
                var attempt = f.Reserve("r1").Attempt;
                Expect(f.Ledger.MarkOutcome("colony_a", attempt.AttemptId, true, "loaded").Success, "Outcome failed.");
                var repeated = f.Ledger.MarkOutcome("colony_a", attempt.AttemptId, true, "loaded");
                Expect(repeated.Success && !repeated.LoadAuthorized && repeated.Consumed == 1, "Repeated outcome is not harmless.");
                ExpectCode(f.Ledger.MarkOutcome("colony_a", attempt.AttemptId, false, "failed"), "rollback_outcome_already_recorded");
                ExpectCode(f.Ledger.MarkOutcome("colony_a", "unknown", true, "loaded"), "rollback_attempt_not_found");
                ExpectCode(f.Ledger.MarkOutcome("colony_b", attempt.AttemptId, true, "loaded"), "rollback_colony_not_registered");
            }
        }

        private static void VerifierFailureAndInvalidCyclesFailClosed()
        {
            using (var f = new Fixture())
            {
                f.Verifier.OnVerify = () => throw new InvalidOperationException("test verifier failure");
                ExpectCode(f.Reserve("r1"), "rollback_verification_failed");
                f.Verifier.OnVerify = null;
                foreach (double cycle in new[] { double.NaN, double.PositiveInfinity, -1d })
                {
                    f.Verifier.Cycle = cycle;
                    ExpectCode(f.Reserve("r1"), "rollback_identity_unverified");
                }
                Expect(f.Ledger.ReadBudget("colony_a").Consumed == 0, "Failed verification consumed a debit.");
            }
        }

        private static void ExpectCode(RollbackLedgerResult result, string expected)
        {
            Expect(!result.Success && !result.LoadAuthorized && result.Attempt == null && result.Code == expected,
                "Expected " + expected + ", received " + result.Code + ".");
        }

        private static void Expect(bool condition, string message)
        {
            if (!condition) throw new InvalidOperationException(message);
        }

        private sealed class Fixture : IDisposable
        {
            public readonly string Root = Path.Combine(Path.GetTempPath(), "oni-rollback-test-" + Guid.NewGuid().ToString("N"));
            public readonly FakeVerifier Verifier = new FakeVerifier();
            public readonly RollbackBudgetLedger Ledger;

            public Fixture(bool initialize = true)
            {
                Ledger = new RollbackBudgetLedger(Root, Verifier);
                if (!initialize) return;
                Expect(Ledger.ProvisionNewStore().Success, "Store provisioning failed.");
                Expect(Ledger.TryRegisterNewColony("colony_a", "session_a").Success, "Enrollment failed.");
            }

            public RollbackLedgerResult Reserve(string requestId) => Ledger.TryReserve("colony_a", "session_a", "native_slot_1", requestId, 3, "test reason");
            public void Dispose() { if (Directory.Exists(Root)) Directory.Delete(Root, true); }
        }

        private sealed class FakeVerifier : IRollbackTargetVerifier
        {
            public string ColonyId = "colony_a";
            public string SessionId = "session_a";
            public bool IsNewColony = true;
            public bool HasAutosave = true;
            public double Cycle = 42.5;
            public Action OnVerify;

            public RollbackVerification VerifyNewColony(string colonyId, string sessionId) =>
                IsNewColony && colonyId == ColonyId && sessionId == SessionId
                    ? RollbackVerification.Accept(Cycle)
                    : RollbackVerification.Deny("rollback_identity_unverified");

            public RollbackVerification VerifyAutosave(string colonyId, string sessionId, string opaqueAutosaveId)
            {
                if (colonyId != ColonyId || sessionId != SessionId) return RollbackVerification.Deny("rollback_identity_unverified");
                if (!HasAutosave) return RollbackVerification.Deny("no_autosave_available");
                if (opaqueAutosaveId != "native_slot_1") return RollbackVerification.Deny("rollback_target_unverified");
                OnVerify?.Invoke();
                return RollbackVerification.Accept(Cycle);
            }
        }
    }
}
