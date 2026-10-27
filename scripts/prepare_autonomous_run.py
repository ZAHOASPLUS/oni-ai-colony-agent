"""Prepare an autonomous run for any named test clone (M1 onwards).

``prepare_dsh_autonomous_run.py`` located its clone by the Phase 2 directory
layout and wrote the controller config with ``validation_process_id: 0`` before
the game was running, which ``oni_autonomous.load_config`` rejects; the anchors
were then patched by hand (``reset_dsh_run.py``).  This script takes the clone
as parameters and splits preparation into the two moments the anchors exist:

``binding`` (game stopped)
    Verifies the clone against its manifest (content, header, isolation,
    ``oni-`` name, manifest run id), resolves what the launch loads (with
    ``--checkpoints`` the newest verified link of the checkpoint chain,
    otherwise the clone), and writes ``<bridge>/runtime-startup-binding.json``
    plus ``<run dir>/prepare-run.json``.  The game reads the binding when it
    loads the save, so it refuses while ``OxygenNotIncluded.exe`` runs.

``config`` (game running, save loaded)
    Re-checks that the binding file is the one this run wrote, finds the
    running game's process id and the Probe's ``session_started`` record that
    carries the same anchors, writes
    ``<run dir>/autonomous-controller-config.v1.json`` and loads it back through
    ``oni_autonomous.load_config``, so a config that would not start the
    controller is reported here instead.

Usage:
    python -B scripts/prepare_autonomous_run.py binding --clone-manifest M --clone-save S [--checkpoints]
    python -B scripts/prepare_autonomous_run.py config --run-id ID [--goal TEXT]

The run id defaults to the manifest's ``run_id``; the run directory defaults to
``reports/m1/runs/<run id>``.  Point the game at the bound save with
``scripts/set_oni_load_save.py`` (``--latest-checkpoint`` with checkpoints).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import sys
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.checkpoint_chain import loader_target  # noqa: E402
from agent.clone_launch_guard import validate_clone_launch  # noqa: E402
from scripts.prepare_dsh_autonomous_run import (  # noqa: E402
    BINDING_SCHEMA,
    BRIDGE,
    CONFIG_SCHEMA,
    DEV_MOD,
    GAME_BUILD,
    MOD_VERSION,
    canonical_hash,
    game_process,
    sha256_bytes,
    sha256_file,
)

PREPARE_SCHEMA = "oni.prepare-run.v2"
CLONE_MANIFEST_SCHEMA = "oni.named-test-clone.v1"
DEFAULT_RUNS_ROOT = ROOT / "reports" / "m1" / "runs"
DEFAULT_CONTROLLER_ID = "oni-autonomous-controller"
DEFAULT_GOAL = (
    "Keep the test colony alive and progressing with conservative, reversible actions: "
    "oxygen, food, water, sanitation, power, temperature, work priorities, research, "
    "building and production, in that order of need."
)
BINDING_FIELDS = frozenset({
    "schema", "validation_run_id", "startup_nonce", "startup_intent_hash",
    "runtime_config_evidence_sha256", "environment_manifest_sha256", "expected_game_build",
    "expected_mod_version", "expected_probe_dll_sha256", "runtime_bootstrap_result_path_id_lower",
    "runtime_bootstrap_result_sha256", "runtime_bootstrap_result_hash", "runtime_bootstrap_transaction_id",
    "installed_probe_tree_content_hash", "requested_loader_path_id_upper",
    "requested_loader_save_sha256", "requested_loader_save_length", "created_at", "document_hash",
})
TRUSTED_START_REASON = "save_loader_load_complete"

GameProcess = Callable[[], Optional[Tuple[int, str]]]


class PrepareError(RuntimeError):
    """A precondition failed; nothing past the failing step was written."""


@dataclasses.dataclass(frozen=True)
class Environment:
    """Where the run's files live; tests substitute a temporary tree."""

    bridge_dir: pathlib.Path = BRIDGE
    mod_dll: pathlib.Path = DEV_MOD
    runs_root: pathlib.Path = DEFAULT_RUNS_ROOT
    game_process: GameProcess = game_process


def _write_atomic(path: pathlib.Path, text: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return payload


def _load_object(path: pathlib.Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrepareError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise PrepareError(f"{label} is not a JSON object")
    return value


def run_dir_for(env: Environment, run_id: str) -> pathlib.Path:
    return env.runs_root / run_id


# -- binding ---------------------------------------------------------------


def build_binding(
    *,
    run_id: str,
    loader: Mapping[str, Any],
    mod_dll: pathlib.Path,
    bootstrap_result: pathlib.Path,
    now: dt.datetime,
) -> Dict[str, Any]:
    """The 19-field ``oni.runtime-startup-binding.v2`` document.

    The loader fields and DLL hash are measured; the evidence anchors this
    machine cannot compute truthfully are run-scoped placeholder hashes, as in
    ``prepare_dsh_autonomous_run``.
    """

    dll_sha = sha256_file(mod_dll)
    binding = {
        "schema": BINDING_SCHEMA,
        "validation_run_id": run_id,
        "startup_nonce": "startup-" + uuid.uuid4().hex,
        "startup_intent_hash": canonical_hash({"run_id": run_id, "intent": "autonomous-run", "nonce": uuid.uuid4().hex}),
        "runtime_config_evidence_sha256": canonical_hash({"run_id": run_id, "runtime_config": "autonomous-controller"}),
        "environment_manifest_sha256": canonical_hash({"run_id": run_id, "environment": f"oni-{GAME_BUILD}"}),
        "expected_game_build": GAME_BUILD,
        "expected_mod_version": MOD_VERSION,
        "expected_probe_dll_sha256": dll_sha,
        "runtime_bootstrap_result_path_id_lower": canonical_hash({"path": str(bootstrap_result).lower()}),
        "runtime_bootstrap_result_sha256": sha256_file(bootstrap_result)
        if bootstrap_result.is_file()
        else canonical_hash({"missing": bootstrap_result.name}),
        "runtime_bootstrap_result_hash": canonical_hash({"run_id": run_id, "bootstrap": "autonomous-run"}),
        "runtime_bootstrap_transaction_id": "rtb-" + uuid.uuid4().hex[:24],
        "installed_probe_tree_content_hash": canonical_hash({
            "tree": "oni-ai-colony-agent",
            "dll": dll_sha,
            "files": sorted(p.name for p in mod_dll.parent.iterdir()),
        }),
        "requested_loader_path_id_upper": loader["path_id_upper"],
        "requested_loader_save_sha256": loader["save_sha256"],
        "requested_loader_save_length": loader["save_length"],
        "created_at": now.isoformat(timespec="microseconds"),
    }
    binding["document_hash"] = canonical_hash(binding)
    if set(binding) != BINDING_FIELDS:
        raise PrepareError(f"binding field set mismatch: {sorted(set(binding) ^ BINDING_FIELDS)}")
    return binding


def prepare_binding(
    env: Environment,
    *,
    clone_manifest: pathlib.Path,
    clone_save: pathlib.Path,
    run_id: Optional[str] = None,
    checkpoints: bool = False,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    clone_manifest = clone_manifest.expanduser().resolve()
    clone_save = clone_save.expanduser().resolve()
    clone_preview = clone_save.with_suffix(".png")
    manifest = _load_object(clone_manifest, "clone manifest")
    if manifest.get("schema") != CLONE_MANIFEST_SCHEMA or manifest.get("created") is not True:
        raise PrepareError("the clone manifest is not a created oni.named-test-clone.v1 manifest")
    manifest_run = manifest.get("run_id")
    run_id = run_id or manifest_run
    if not isinstance(run_id, str) or run_id != manifest_run:
        # oni_autonomous.load_config refuses a manifest of another run.
        raise PrepareError(f"run id {run_id!r} is not the clone manifest's run id {manifest_run!r}")
    if not clone_save.stem.lower().startswith("oni-"):
        raise PrepareError("the test clone must use an explicit oni- name")
    if not env.mod_dll.is_file():
        raise PrepareError(f"deployed probe DLL missing: {env.mod_dll}")
    if env.game_process() is not None:
        raise PrepareError("OxygenNotIncluded.exe is running; the game reads the binding when it loads, stop it first")
    try:
        validate_clone_launch(clone_manifest, clone_save, clone_preview, bridge_dir=env.bridge_dir)
    except Exception as exc:  # the guard's own error types; reported, never retried
        raise PrepareError(f"clone check failed: {exc}") from exc

    if checkpoints:
        loader = loader_target(clone_manifest, clone_save)
    else:
        from agent.checkpoint_chain import loader_path_id_upper

        loader = {
            "index": 0, "save_path": clone_save, "save_sha256": sha256_file(clone_save),
            "save_length": clone_save.stat().st_size, "path_id_upper": loader_path_id_upper(clone_save),
            "chain": [],
        }

    now = now or dt.datetime.now(dt.timezone.utc).astimezone()
    binding = build_binding(
        run_id=run_id, loader=loader, mod_dll=env.mod_dll,
        bootstrap_result=env.bridge_dir / "v27-runtime-bootstrap.json", now=now,
    )
    binding_path = env.bridge_dir / "runtime-startup-binding.json"
    binding_raw = _write_atomic(binding_path, json.dumps(binding, indent=2, sort_keys=True) + "\n")
    record = {
        "schema": PREPARE_SCHEMA,
        "phase": "binding",
        "run_id": run_id,
        "prepared_at": now.isoformat(timespec="seconds"),
        "clone_manifest": str(clone_manifest),
        "clone_manifest_sha256": sha256_file(clone_manifest),
        "clone_save": str(clone_save),
        "clone_preview": str(clone_preview),
        "checkpoints": checkpoints,
        "loader": {
            "save": str(loader["save_path"]),
            "index": loader["index"],
            "save_sha256": loader["save_sha256"],
            "path_id_upper": loader["path_id_upper"],
        },
        "binding": {
            "path": str(binding_path),
            "sha256": sha256_bytes(binding_raw),
            "document_hash": binding["document_hash"],
            "startup_intent_hash": binding["startup_intent_hash"],
            "runtime_config_evidence_sha256": binding["runtime_config_evidence_sha256"],
            "expected_probe_dll_sha256": binding["expected_probe_dll_sha256"],
        },
        "bridge_dir": str(env.bridge_dir),
    }
    run_dir = run_dir_for(env, run_id)
    _write_atomic(run_dir / "prepare-run.json", json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


# -- config ----------------------------------------------------------------


def _session_mismatches(record: Mapping[str, Any], prepared: Mapping[str, Any], pid: int) -> list:
    binding = prepared["binding"]
    loader = prepared["loader"]
    expected = {
        "schema": "oni.event.v2",
        "validation_process_id": pid,
        "validation_run_id": prepared["run_id"],
        "startup_binding_hash": binding["sha256"],
        "startup_intent_hash": binding["startup_intent_hash"],
        "runtime_config_evidence_sha256": binding["runtime_config_evidence_sha256"],
        "game_build": GAME_BUILD,
        "mod_version": MOD_VERSION,
        "startup_binding_status": "verified",
        "reason": TRUSTED_START_REASON,
        "active_save_path_id": loader["path_id_upper"],
        "save_id_hash": loader["save_sha256"],
    }
    return sorted(name for name, value in expected.items() if record.get(name) != value)


def find_session(state_log: pathlib.Path, prepared: Mapping[str, Any], pid: int) -> Dict[str, Any]:
    """The newest ``session_started`` of ``pid`` whose anchors all match.

    A record of the right process with a mismatch is reported field by field,
    which tells a tester which half of the binding the Probe disagreed with.
    """

    if not state_log.is_file():
        raise PrepareError(f"Probe state log missing: {state_log}")
    matched: Optional[Dict[str, Any]] = None
    nearest: Optional[Tuple[Dict[str, Any], list]] = None
    with state_log.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if '"session_started"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("event") != "session_started":
                continue
            if record.get("validation_process_id") != pid:
                continue
            mismatches = _session_mismatches(record, prepared, pid)
            if mismatches:
                nearest = (record, mismatches)
            else:
                matched, nearest = record, None
    if matched is not None and nearest is None:
        session_id = matched.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise PrepareError("the matching session_started record has no session id")
        return matched
    if nearest is not None:
        raise PrepareError(
            f"the newest session_started of process {pid} does not match this run's binding: "
            + ", ".join(nearest[1])
        )
    raise PrepareError(f"no session_started record for process {pid} yet; wait for the save to finish loading")


def prepare_config(
    env: Environment,
    *,
    run_id: str,
    controller_id: str = DEFAULT_CONTROLLER_ID,
    goal: str = DEFAULT_GOAL,
) -> Dict[str, Any]:
    run_dir = run_dir_for(env, run_id)
    prepared = _load_object(run_dir / "prepare-run.json", "prepare record")
    if prepared.get("schema") != PREPARE_SCHEMA or prepared.get("run_id") != run_id:
        raise PrepareError("the prepare record is not this run's binding record")
    bridge_dir = pathlib.Path(prepared["bridge_dir"])
    binding_path = pathlib.Path(prepared["binding"]["path"])
    if not binding_path.is_file() or sha256_file(binding_path) != prepared["binding"]["sha256"]:
        raise PrepareError("the startup binding changed since the binding phase; prepare again with the game stopped")
    if sha256_file(pathlib.Path(prepared["clone_manifest"])) != prepared["clone_manifest_sha256"]:
        raise PrepareError("the clone manifest changed since the binding phase")
    process = env.game_process()
    if process is None:
        raise PrepareError("OxygenNotIncluded.exe is not running; start it and load the bound save first")
    pid = process[0]
    session = find_session(bridge_dir / "state.jsonl", prepared, pid)

    config = {
        "schema": CONFIG_SCHEMA,
        "bridge_dir": str(bridge_dir),
        "clone_manifest": prepared["clone_manifest"],
        "clone_manifest_sha256": prepared["clone_manifest_sha256"],
        "clone_save": prepared["clone_save"],
        "clone_preview": prepared["clone_preview"],
        "run_id": run_id,
        "controller_id": controller_id,
        "session_id": session["session_id"],
        "world_id": 0,
        "startup_expectations": {
            "validation_run_id": run_id,
            "validation_process_id": pid,
            "startup_binding_hash": prepared["binding"]["sha256"],
            "startup_intent_hash": prepared["binding"]["startup_intent_hash"],
            "runtime_config_evidence_sha256": prepared["binding"]["runtime_config_evidence_sha256"],
            "loader_path_id_upper": prepared["loader"]["path_id_upper"],
            "loader_save_sha256": prepared["loader"]["save_sha256"],
            "game_build": GAME_BUILD,
            "mod_version": MOD_VERSION,
        },
        "goal": goal,
        "poll_seconds": 5.0,
        "model_deadline_seconds": 120.0,
    }
    if prepared.get("checkpoints") is True:
        config["checkpoints"] = True
    config_path = run_dir / "autonomous-controller-config.v1.json"
    _write_atomic(config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")

    from scripts.oni_autonomous import load_config

    try:
        loaded = load_config(config_path)
    except (ValueError, OSError) as exc:
        raise PrepareError(f"the written config does not load: {exc}") from exc
    report = {
        "schema": PREPARE_SCHEMA,
        "phase": "config",
        "run_id": run_id,
        "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
        "game_process_id": pid,
        "session_id": session["session_id"],
        "save_id_hash": loaded["save_id_hash"],
        "checkpoint_index": loaded.get("checkpoint_index", 0),
        "next": (
            "python -B scripts/write_dsh_autonomous_policy.py "
            f"--run-id {run_id} --controller-id {controller_id} --session {session['session_id']} "
            f"--world 0 --save-hash {loaded['save_id_hash']} --config {config_path}"
        ),
    }
    _write_atomic(run_dir / "prepare-config.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main(argv: Optional[list] = None, env: Optional[Environment] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="phase", required=True)
    binding = sub.add_parser("binding", help="write the startup binding (game stopped)")
    binding.add_argument("--clone-manifest", required=True, type=pathlib.Path)
    binding.add_argument("--clone-save", required=True, type=pathlib.Path)
    binding.add_argument("--run-id", default=None, help="defaults to the manifest's run_id")
    binding.add_argument("--checkpoints", action="store_true",
                         help="load the newest checkpoint of the clone and save the next one on stop")
    config = sub.add_parser("config", help="write the controller config (game running, save loaded)")
    config.add_argument("--run-id", required=True)
    config.add_argument("--controller-id", default=DEFAULT_CONTROLLER_ID)
    config.add_argument("--goal", default=DEFAULT_GOAL)
    for item in (binding, config):
        item.add_argument("--runs-root", type=pathlib.Path, default=None,
                          help=f"defaults to {DEFAULT_RUNS_ROOT.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    env = env or Environment()
    if args.runs_root is not None:
        env = dataclasses.replace(env, runs_root=args.runs_root.expanduser().resolve())
    try:
        if args.phase == "binding":
            report = prepare_binding(
                env, clone_manifest=args.clone_manifest, clone_save=args.clone_save,
                run_id=args.run_id, checkpoints=args.checkpoints,
            )
        else:
            report = prepare_config(env, run_id=args.run_id, controller_id=args.controller_id, goal=args.goal)
    except PrepareError as exc:
        print(json.dumps({"schema": PREPARE_SCHEMA, "phase": args.phase, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
