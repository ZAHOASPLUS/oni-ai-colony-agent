# Controller integration with an existing Colony Plan

`agent.controller_bridge.create_colony_plan_controller` binds the controller to
one explicitly selected leaf of an existing active Colony Plan. It publishes at
most one **planned, pending human approval** command through the existing plan
owner and reads the resulting command's confirmation. It does not approve or
execute commands, advance a successor, launch the game, or grant live admission.

The first offline integration milestone is covered by
`python scripts/test_controller_bridge.py` (28 cases). The test uses temporary directories
and real plan, queue, approval-builder, outcome and runtime-confirmation code;
Probe snapshots and executor ACKs in that test are synthetic. A test pass does
not establish a game effect.

## API

Supply an explicit Bridge directory, existing plan and step IDs, the complete
trusted runtime identity, and an existing private intent directory. No path,
identity, action policy, test save or authorization is auto-discovered.

```python
from pathlib import Path
from agent.controller_bridge import create_colony_plan_controller

# Values below come from the reviewed session and selected existing plan.
# The intent directory must already exist and remain available across restarts.
core = create_colony_plan_controller(
    Path(bridge_directory),
    plan_id=selected_plan_id,
    step_id=selected_step_id,
    expected_identity={
        "session_id": trusted_session_id,
        "save_id_hash": trusted_test_save_hash,
        "world_id": trusted_world_id,
        "game_build": trusted_game_build,
        "mod_version": trusted_mod_version,
    },
    intent_directory=Path(private_intent_directory),
    allow_pending_commands=True,
)
cycle = core.step()
proposal = cycle.decision.proposal
```

The default `allow_pending_commands=False` rejects publication. To inspect
without consuming an intent, use `core.adapter.observe()` and
`core.adapter.policy()`; `step()` is the intent-consuming operation. The caller
must keep the existing live environment/clone admission checks ahead of any
publication-enabled call. Current M29 unknown-result and
`configured_not_restored` evidence still prevents claiming live readiness.

## Waiting, confirmation and restart

The first `step()` performs one bounded publication attempt. A PENDING result
means that the unique real command is awaiting approval, execution or final
confirmation. After external progress, later calls to `step()` read its effect;
they never call `apply()` again. The maximum is three effect observations in
total, including the initial call. Waiting beyond this budget stops the Core.
Do not use rapid repeated calls as a scheduler.

Keep the original `ActionProposal` from the first cycle. Its `as_dict()` can be
stored with the calling application's recovery evidence. The adapter's
`reconcile(proposal)` is a separate read-only inspection operation available
after the Core's budget is exhausted. It does not resume a stopped Core or
publish another command. Recreating a Core with the same durable intent
directory also cannot resend its consumed intent. This API does not yet supply
a persisted runner/recovery command-line interface.

An APPLIED result requires a unique real `commands/done` command with status
`confirmed`, matching selected plan/leaf/proposal, agreeing session outcome
certificates, ordered accepted/executed/local-effect ACKs and a later trusted
archived witness. Core separately checks that witness against expected effects.
A matching latest state, an execution receipt, `done/verified`, prepared
`confirming/confirmed`, missing evidence or an unknown result cannot substitute
for that chain. Evidence is captured through bounded regular-file reads and
rechecked after assessment; replacements, duplicates and links fail closed.

The current capture supports individual JSON files up to 4 MiB, each complete
session `state.jsonl` up to 64 MiB, and 128 MiB of total confirmation evidence.
An otherwise valid larger archive yields UNKNOWN. This is an explicit current
size limit; stable streaming selection of only required ACK/witness records is
future work. Do not truncate a real archive to make it pass.

Only the selected leaf is integrated. A satisfied/noop Core result does not
complete its Colony Plan leaf, and a confirmed command does not automatically
advance the plan revision or its successor. `ColonyPlanControllerSequence`
coordinates an explicit ordered list of leaves: it creates one adapter/Core
per leaf, waits for the current leaf's revision writeback, and only then lets a
later call inspect the next leaf. It is bounded to 32 leaves and three calls per
leaf by default. A satisfied leaf is committed with
`already_satisfied_without_command`; it never creates a fake command or
confirmation. Approval and execution remain external.

## External model decision binding

`agent.llm_planner.colony_plan_definition_from_plan` is the only supported
projection from a planner result into a Colony Plan definition. It accepts a
successful `oni.plan.v2` result and the same local advice collection used to
produce it. The model can change the order of existing incident IDs, but every
step's action, arguments, preconditions, and expected effects are copied from
the local command contract. Missing contracts, an unconfirmed model result, or
an unsupported planner mode raises an error before plan creation. The resulting
definition carries bounded `oni.llm-decision-provenance.v1` metadata with
prompt, incident, and response hashes plus the model name; credentials and raw
provider responses are never persisted. `create_colony_plan` validates and
stores that provenance. When the planner includes its source snapshot, creation
also rechecks the exact session/save/world/snapshot/state hash against the fresh
Bridge state and rejects stale results. The normal approval, custody,
confirmation, and clone-only gates remain unchanged.

For a file-backed workflow, `scripts/manage_colony_plan.py create-from-llm`
accepts `--planner-plan` and `--advice` JSON files and performs this projection
before calling `create_colony_plan`; it does not approve, execute, or launch
the game.

The future clone launcher must call `agent.clone_launch_guard.validate_clone_launch`
first. This read-only guard verifies the named-test-clone manifest, exact save
and preview hashes, source-path separation, protected process counts, and empty
command/validation queues. It has no process-launch or mutation API; its
fixture-only regression is `scripts/test_clone_launch_guard.py` (4 cases).

M29 recovery also depends on Windows extended-length paths. Transaction owner
capture now traverses and lstat's descendants through the extended path adapter;
`scripts/test_transaction_tree_long_path.py` exercises a 388-character fixture
and confirms the complete directory identity set without touching the live
environment. A missing frozen release artifact was restored from the verified
installed Probe to its exact workspace path with a hash/length preimage; the
subsequent abort what-if correctly stopped at the historical identity/mtime
custody check, so no recovery replay is implied.
