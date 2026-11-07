# Game-independent Agent harness

This executable links the production context, runtime, HTTP client, configuration,
credential store, rollback ledger, schema validator and write-lease sources. It
uses mocked model/HTTP/dispatcher implementations and temporary ledger files. It
does not launch ONI, install a Mod, send a network request or use a real API key.

## Run prerequisites

- An already installed .NET 8 SDK.
- An already available compatible `Newtonsoft.Json.dll`. The default location is
  the ONI Managed directory. No NuGet PackageReference or package download is used.

From the repository root:

```powershell
dotnet run --project tools/AgentRuntimeHarness/AgentRuntimeHarness.csproj -p:ONIManagedDir="C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
```

Or specify the JSON assembly directly:

```sh
dotnet run --project tools/AgentRuntimeHarness/AgentRuntimeHarness.csproj -p:NewtonsoftJsonPath=/absolute/path/to/Newtonsoft.Json.dll
```

The repository's normal Mod build remains a separate `netstandard2.1` build and
requires the ONI and Unity reference assemblies. Running this `net8.0` harness is
not a substitute for that build.

## Runtime cases

- Whole assistant/tool exchanges survive trimming together; result truncation is
  explicit valid JSON and context snapshots cannot mutate retained history.
- The latest player correction appears after superseded instructions.
- Serial multi-call execution produces one result per tool-call ID.
- A late non-cooperative model response cannot act after correction, stop or
  session replacement; no overlapping model requests are spawned.
- Correction during a running handler records its result, drops the unexecuted
  remainder and never forwards a partial exchange to the next model call.
- A mocked queued dispatcher rechecks generation before execution, independently
  of asynchronous cancellation callback timing.
- Pause cancels pending model work without changing game speed. Resume and
  correction read fresh state before replanning. Failed refresh prevents planning.
- Per-tool validation/execution failures become matching tool results; an
  oversized batch is rejected before any model-requested tool is executed.
- Model-turn, total-tool and wall-clock budgets are enforced. Corrections and
  resumes do not reset consumed budgets; a new Start does. Refresh calls count
  toward the total-tool budget.
- Sanitized model failure code and HTTP status remain visible; arbitrary exception
  text is not copied into the model history or runtime event.
- The process-local write lease permits only one session owner.

Client, ledger and schema tests provide their own additional scenarios. Harness
failure exits nonzero. Every asynchronous runtime test has a five-second deadline
so an unexpected stall is reported rather than hanging the test process.

## Verification status and limits

At initial implementation on 2026-11-07, this environment had no `dotnet`, `csc`
or `mcs`. The project XML, source references and whitespace checks were inspected,
but this harness and the Mod were **not compiled or executed** here.

Even a future harness pass will not validate the production Unity dispatcher,
real main-thread execution or waits, lifecycle hooks, console UI, actual model
providers, DPAPI round trips on Windows, new-game candidate APIs, or native
autosave/rollback loading. Those require the separate Mod build and controlled
game integration checks. Runtime uses one background worker: a transport that
ignores cancellation delays the next generation instead of creating an unsafe
parallel request. The real client supplies its own cancellation and deadline.
