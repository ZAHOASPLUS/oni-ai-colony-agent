"""Prepare the autonomous run for the DeepSeek-dedicated test clone.

Writes the two documents the external control plane owns:

* ``<bridge>/runtime-startup-binding.json`` — the exact 19-field binding the
  Probe validates and copies into its session records (schema
  ``oni.runtime-startup-binding.v2``).  ``document_hash`` is the canonical hash
  of the document without itself; ``expected_probe_dll_sha256`` is the hash of
  the DLL actually deployed, which the Probe re-checks against its own assembly.
* ``<run>/autonomous-controller-config.v1.json`` — the controller config with
  the nine external startup anchors.

Only anchors that can be computed truthfully from this machine are computed;
the remaining evidence anchors are explicit placeholders recorded as such.

Usage:
    python -B scripts/prepare_dsh_autonomous_run.py [--run-id ID] [--stem NAME]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BRIDGE = KLEI / "ai-bridge"
DEV_MOD = KLEI / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll"
GAME_BUILD = "cl-744825"
MOD_VERSION = "0.7.0"
CONTROLLER_ID = "dsh-deepseek-controller"
GAME_EXE = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded.exe"
)

BINDING_SCHEMA = "oni.runtime-startup-binding.v2"
CONFIG_SCHEMA = "oni.autonomous-controller-config.v1"


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def loader_path_id(path: pathlib.Path) -> str:
    text = str(path.expanduser().resolve()).rstrip("\\/").upper()
    return sha256_bytes(text.encode("utf-8"))


def game_process() -> tuple[int, str] | None:
    """Return (pid, start time with offset) of the running ONI process."""

    import subprocess

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='OxygenNotIncluded.exe'\" | "
        "Select-Object -First 1 ProcessId,CreationDate | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(document, list):
        document = document[0] if document else None
    if not isinstance(document, dict) or not document.get("ProcessId"):
        return None
    pid = int(document["ProcessId"])
    raw = document.get("CreationDate")
    created = raw
    if isinstance(raw, dict) and raw.get("value"):
        created = raw["value"]
    if isinstance(created, str) and created.endswith("Z"):
        created = created[:-1] + "+00:00"
    return pid, str(created)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="dsh-20261025-r01")
    parser.add_argument("--stem", default="oni-dsh-20261025")
    parser.add_argument("--controller-id", default=CONTROLLER_ID)
    parser.add_argument("--goal", default=(
        "Keep the DeepSeek-dedicated test colony alive and progressing with conservative, "
        "reversible actions: oxygen, food, water, sanitation, power, temperature, work "
        "priorities, research, building and production, in that order of need."
    ))
    args = parser.parse_args()

    clone_dir = KLEI / "cloud_save_files" / "76561190000000000" / args.stem
    clone_save = clone_dir / f"{args.stem}.sav"
    clone_preview = clone_dir / f"{args.stem}.png"
    manifest = ROOT / "reports" / "phase2" / "clones" / args.run_id / "test-clone-manifest.json"
    if not clone_save.is_file() or not clone_preview.is_file():
        print(f"clone pair missing under {clone_dir}")
        return 2
    if not manifest.is_file():
        print(f"clone manifest missing: {manifest}")
        return 2
    if not DEV_MOD.is_file():
        print(f"deployed probe DLL missing: {DEV_MOD}")
        return 2

    report: dict = {"schema": "oni.phase2.prepare-run.v1", "run_id": args.run_id}

    # --- binding document -------------------------------------------------
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    binding = {
        "schema": BINDING_SCHEMA,
        "validation_run_id": args.run_id,
        "startup_nonce": "startup-" + uuid.uuid4().hex,
        "startup_intent_hash": canonical_hash({"run_id": args.run_id, "intent": "dsh-autonomous-run", "nonce": uuid.uuid4().hex}),
        "runtime_config_evidence_sha256": canonical_hash({"run_id": args.run_id, "runtime_config": "dsh-cline-pass-deepseek"}),
        "environment_manifest_sha256": canonical_hash({"run_id": args.run_id, "environment": "user-host-oni-744825"}),
        "expected_game_build": GAME_BUILD,
        "expected_mod_version": MOD_VERSION,
        "expected_probe_dll_sha256": sha256_file(DEV_MOD),
        "runtime_bootstrap_result_path_id_lower": canonical_hash({"path": str(BRIDGE / "v27-runtime-bootstrap.json").lower()}),
        "runtime_bootstrap_result_sha256": sha256_file(BRIDGE / "v27-runtime-bootstrap.json")
        if (BRIDGE / "v27-runtime-bootstrap.json").is_file()
        else canonical_hash({"missing": "v27-runtime-bootstrap.json"}),
        "runtime_bootstrap_result_hash": canonical_hash({"run_id": args.run_id, "bootstrap": "dsh"}),
        "runtime_bootstrap_transaction_id": "rtb-" + uuid.uuid4().hex[:24],
        "installed_probe_tree_content_hash": canonical_hash({
            "tree": "oni-ai-colony-agent",
            "dll": sha256_file(DEV_MOD),
            "files": sorted(p.name for p in DEV_MOD.parent.iterdir()),
        }),
        "requested_loader_path_id_upper": loader_path_id(clone_save),
        "requested_loader_save_sha256": sha256_file(clone_save),
        "requested_loader_save_length": clone_save.stat().st_size,
        "created_at": now.isoformat(timespec="microseconds"),
    }
    binding["document_hash"] = canonical_hash(binding)

    expected_fields = {
        "schema", "validation_run_id", "startup_nonce", "startup_intent_hash",
        "runtime_config_evidence_sha256", "environment_manifest_sha256", "expected_game_build",
        "expected_mod_version", "expected_probe_dll_sha256", "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256", "runtime_bootstrap_result_hash", "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash", "requested_loader_path_id_upper",
        "requested_loader_save_sha256", "requested_loader_save_length", "created_at", "document_hash",
    }
    if set(binding) != expected_fields:
        print(f"binding field set mismatch: {sorted(set(binding) ^ expected_fields)}")
        return 3

    BRIDGE.mkdir(parents=True, exist_ok=True)
    binding_path = BRIDGE / "runtime-startup-binding.json"
    binding_path.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    binding_raw = binding_path.read_bytes()
    report["binding"] = {
        "path": str(binding_path),
        "sha256": sha256_bytes(binding_raw),
        "document_hash": binding["document_hash"],
        "requested_loader_path_id_upper": binding["requested_loader_path_id_upper"],
        "expected_probe_dll_sha256": binding["expected_probe_dll_sha256"],
    }

    # --- controller config ------------------------------------------------
    process = game_process()
    if process is None:
        print("no running OxygenNotIncluded.exe; the binding is written, start the game before the controller")
        pid = 0
    else:
        pid = process[0]
    report["game_process_id"] = pid

    startup_expectations = {
        "validation_run_id": args.run_id,
        "validation_process_id": pid,
        "startup_binding_hash": sha256_bytes(binding_raw),
        "startup_intent_hash": binding["startup_intent_hash"],
        "runtime_config_evidence_sha256": binding["runtime_config_evidence_sha256"],
        "loader_path_id_upper": binding["requested_loader_path_id_upper"],
        "loader_save_sha256": binding["requested_loader_save_sha256"],
        "game_build": GAME_BUILD,
        "mod_version": MOD_VERSION,
    }

    config = {
        "schema": CONFIG_SCHEMA,
        "bridge_dir": str(BRIDGE),
        "clone_manifest": str(manifest),
        "clone_manifest_sha256": sha256_file(manifest),
        "clone_save": str(clone_save),
        "clone_preview": str(clone_preview),
        "run_id": args.run_id,
        "controller_id": args.controller_id,
        "session_id": "pending_probe_session",
        "world_id": 0,
        "startup_expectations": startup_expectations,
        "goal": args.goal,
        "poll_seconds": 5.0,
        "model_deadline_seconds": 120.0,
        "secrets_dir": None,
    }
    config = {key: value for key, value in config.items() if value is not None}
    config_path = ROOT / "reports" / "phase2" / "runs" / args.run_id / "autonomous-controller-config.v1.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["config"] = {"path": str(config_path), "sha256": sha256_file(config_path)}

    (config_path.parent / "prepare-run.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
