# C2a isolated real-resource acceptance plan

Action A09A9-C2A-REAL-PLAN-01, 2026-09-07T10:42Z. Conditional design only.
This document does not dispatch a test or admit the production verifier/game.
Astra root 01a07b6c-9921-7f72-9660-f7b1364d8acb owns acceptance. The sole
C2a code writer is ONI C2a 资源所有权原子实现与 fake 验证,
01a07b70-f8ee-7763-b713-d75f0b343686. Its phase is still active. No root test
may import or execute a changing source revision while that writer is active.

## Preconditions before a separate execution checkpoint

The writer must be terminal, with its two exact preimages and all failed
reports retained. Root must independently inspect allowed AST differences,
the actual fake harness and its positive/fault results. Accepted C1 predicates,
protocol, inventory, case bodies, CLI and six protected sources must be unchanged.
Root must freeze the final source/doc/harness hashes, refresh process/transport
evidence, establish no other writer, and name the one next action. C1 preimages
for this repair were D2985BF47225672FEC266CBCA721EEAA7D97AFD9991F24662F3D715451470A28
and D5A1024F075D8DDA4229858BFB4E2FC6580D0965F3417E37C86C521CF0FF2549;
they are not the future accepted C2a hashes.

The real harness must be separately authored/reviewed after source freeze.
Before execution, root must publish its absolute path/hash, exact command,
bounded duration, owned Temp root, process evidence and cleanup/rollback plan.
Record original save/preview and normal Mod/config inventory before/after any
run whose completion claims depend on their preservation. A missing inventory
is unverified, never an implied no-drift result.

## Isolation and evidence scope

AST-extract the accepted resource functions/classes into an explicit namespace;
do not import verifier top-level, agent modules or fixtures. Inject an inert
child launcher which retains the requested command as evidence but launches
only the separately reviewed child helper with the current Python executable,
-B, shell=False, hidden window and pipes. The substituted child command is
recorded explicitly; this is not evidence for the real verifier worker CLI.

The inert child uses standard library only, emits a bounded known protocol,
waits for one stdin line or EOF, and exits. Expected op/id/attestation values
come from the test contract, not production execution. No ONI/Klei/Steam/Bridge,
normal Mod/config, real command queue or save loading is allowed. Lifecycle,
sample and fixture dependencies stay inert; no cross-process lock claim follows.

Use one new direct child of the actual system Temp directory with a nonempty
nonce. Validate its resolved absolute location and absence of reparse ancestry
before creation/use. Keep only exact owned paths and actual Popen objects;
never recover ownership by process name, glob, broad Temp deletion or PID alone.
The source resource functions use real marker files, stdio pipes, reader
threads and monotonic timing inside that owned directory. Fault wrappers must
delegate non-failing operations to those actual resources.

Persistent evidence uses a separate exclusive reports directory and open('x').
Every retry uses a new report name and retains failed attempts. No source edit
is allowed during a real test. Unexpected safety-gate acceptance or unowned
mutation stops subsequent cases at the cleanup checkpoint.

## Initial finite case matrix

One clean positive must complete first with zero exit, both real readers at
EOF and joined, no cleanup errors, one go, and the owned marker removed.
Then run only the independently reviewed cases needed to establish facts
that fake resources cannot prove:

1. Popen failure after actual marker creation: no child existed; marker owned
   cleanup observed; primary launch error preserved.
2. First reader construction/start failure before actual start: actual child
   receives EOF after stdin closes; exact Popen exits/reaps; no invented EOF
   for the missing reader; inactive owned pipes handled individually.
3. Second reader construction/start failure with the first real reader active:
   first reader remains registered, child exits, existing reader drains/joins,
   no never-started join, no live-reader pipe close.
4. Baseline failure after real ready and before go: zero go writes, stdin
   closes before wait, child exits on EOF, launch error retained.
5. Injected flush failure after delegating a real successful flush: the child
   may have received go. Observe at most one go write and retain uncertainty;
   no retry may be used to manufacture completion.
6. One deliberately non-cooperative inert child: the existing timeout expires,
   only the retained Popen handle is killed, bounded wait completes and the
   PID is gone. forced_kill remains failed verifier evidence even when this
   fault-test expectation matches.
7. Owned marker replaced/disappeared before cleanup: preservation/fault and
   final presence are observed on disk; replacement material is never deleted.

Root may omit redundant real cases only by naming the evidence already
established and retaining the fake-only limitation. Do not enlarge this matrix
to production races, unsupported CLI paths, inventory, or actual game actions.

## Required observations and terminal checks

Each record binds source/harness/child hashes, unique case/root identity,
primary exception, actual child PID plus creation evidence, actual return code,
stdin/go call ordering, real reader EOF/error/aliveness/counts, pipe cleanup,
marker creation/identity/content checks/final presence, forced kill and residual
resources. Record wall-clock start/end and the declared per-phase/total bound.
Missing readers remain incomplete evidence; JSON must not include live objects.

Outer tool process exit is independent evidence and must be retained outside
any report's self-authored normal_exit field. Poll a retained process handle
at most every 30 seconds. On ambiguous tool delivery inspect that handle,
owned PID/root and log cursor; never replay the case merely because output
is blank. A case log must not be mistaken for a completed suite.

After all selected cases, remeasure source/doc/harness and protected hashes,
verify all exact spawned PIDs have exited and all readers/helpers are gone,
and inspect only the owned Temp tree. Preserve failure/replacement evidence
until its hashes/report are sealed. Any cleanup removal must validate the
resolved exact target inside the owned root, using one native shell end to end.
Do not erase unrelated historical Temp evidence. Rollback of production code
is not included; a source rollback requires a separate hash-guarded action.

The fault suite can pass because an injected fault is correctly detected and
contained while that case's verifier result remains failed. Report these two
statuses separately. Even complete success establishes only isolated Windows
resource handling. C2b inventory/protocol, four-case real fixture baseline,
production confirmation custody, C# interoperability, bootstrap, live G0 and
the full survival roadmap remain separate outstanding gates.
