# C2a four-cleanup fake acceptance supplement

Action A09A9-C2A-FAKE-SUPPLEMENT-01, 2026-09-07T11:52Z.
Design only. This supplements the immutable independent fake contract
`a09a9c2a-independent-fake-contract-20260907.md` at SHA256
85F22828E73F93B38A2C42BBCE46F0EFF96300D296DCBA510E20CFF545789183.
All its extraction, isolation, 25 original cases, A-D additions, budgets,
preservation and report-binding requirements remain mandatory.

The current source author is the sole Luna Max task
01a07bb0-c677-72d0-ad05-fbceac90918f. The root commander is Astra Ultra task
01a07baf-041f-7cb0-9007-883a367af818. Source authoring is still active; this
document neither freezes its future result nor permits execution. Root must
first verify terminal status, actual source/doc/preimage hashes and minimal
AST changes, then dispatch a fresh bounded helper author. Do not resume an
old writer or use an old helper/report as accepted evidence.

## Separate the three outcomes

Every case records `fault_expectation_matched`, `verifier_evidence_passed`
and `cleanup_complete` independently. The complete positive uses a no-fault
expectation and must set all three true. An injected launch, cleanup or kill
fault cannot become passed verifier evidence because its expected outcome
matched. Resource recovery on a later cleanup attempt cannot erase history.

For this resource harness, verifier evidence includes actual protocol validity,
the actual C1 `_worker_evidence_valid` and its frozen AST dependencies, complete
reader evidence, zero exit, no forced kill, no primary/cleanup/reader errors,
no overflow/stderr and no residual resources. `_worker_evidence_valid` alone
checks only its own evidence contract, not this entire conjunction or the
production case/global gate. Report that scope accurately.

`cleanup_complete` needs observed fake child exit/reaping, all owned handles
closed, required readers inactive with EOF evidence, and resolved marker
ownership/presence. For construction failure, a reader proven never to have
started can leave its EOF unavailable while owned resources are all closed;
report that distinction and keep verifier evidence false. Unknown reader
activity or EOF where a reader may have run keeps cleanup incomplete.

## Additional precise cases

1. **Missing reader, known never started.** Traverse actual `_spawn_worker`
   registration and fail first and second reader construction before any
   thread start for that reader. Assert separately that each actually owned
   pipe is closed, other cleanup continues, no never-started join occurs,
   missing-reader EOF stays unavailable, and verifier evidence fails.
   Test missing ownership/start keys and explicit unknown states separately.
   Defaults inserted by `_ensure_worker_ledger` are not original ownership or
   no-start observations. Such unknown cases must preserve the affected
   stream, avoid unowned operations and report residual uncertainty.

2. **Final getter failures before sealing.** Trace final field reads and the
   result seal boundary. The complete positive has no fallible reader getter
   after sealing. Inject a precise first failure on final `error`,
   `lines_read`, `bytes_read`, `dropped_lines`, and EOF observation, with
   earlier reads explicitly successful when applicable. Each designated
   getter must actually be reached. The exact error is retained before all
   result error/phase fields are sealed; phase cannot remain cleaned with
   cleanup_error=None. Final fields must come from one guarded snapshot, not
   another getter after error collection. Keep physical resource recovery
   distinct from the missing/error observation evidence.

3. **Cleanup retry history.** A close fails before taking effect on attempt
   one, then succeeds on attempt two. First-attempt cumulative errors remain
   an ordered prefix, current residuals become empty only after observed
   closure, and verifier evidence stays false. Separately force a kill on
   attempt one and invoke cleanup again after reaping. Historical forced_kill
   remains true. Record each attempt's errors, resource result, kill action
   and cumulative history. Include a residual/unknown variant. Repeated
   cleanup must not invent a new marker, overwrite a retained owned handle,
   kill an unowned process or delete a replacement.

4. **Start failure plus secondary observation failure.** Exercise actual
   `_PipeReader.start`. Separately inject secondary `is_alive` and `ident`
   errors while handling the original start exception. Specify whether the
   start operation took effect before raising. The original exception stays
   first, the exact secondary error remains visible through ledger and
   cleanup, and started stays unknown. A fallback getter must not silently
   turn explicit unknown into False. Affected alive/unproven streams remain
   open while independently owned safe resources continue cleanup. Include
   the control where reliable observations establish never-started and the
   affected owned stream can safely close; verifier evidence remains false.

Use explicit trace/counter assertions for error ordering, operation effects,
read counts and resource continuation. A NameError, unrelated exception,
unreached getter, automatic settle, manufactured EOF or loss of historical
errors is a harness/case failure, never a matching negative. Keep the original
source timeouts and record iterations as well as virtual elapsed time.

The root independently reviewed these oracles with a separate read-only
auditor. They do not establish real Windows scheduling, blocking reads or
cleanup. Even eventual fake success leaves inert real resources, C2b,
production confirmation/C#, integration, bootstrap, live G0 and the full
sustainable survival objective pending.
