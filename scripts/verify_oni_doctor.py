#!/usr/bin/env python3
"""Offline contract checks for :mod:`scripts.oni_doctor`.

The fixture uses temporary files only.  It never launches ONI, touches a real
save, calls the network, or writes to the project reports directory.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Dict

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import oni_doctor as doctor


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_json(path: pathlib.Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def healthy_fixture(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    steam_root = root / "Steam"
    oni_dir = steam_root / "steamapps" / "common" / "OxygenNotIncluded"
    managed = oni_dir / "OxygenNotIncluded_Data" / "Managed"
    managed.mkdir(parents=True)
    (oni_dir / "OxygenNotIncluded.exe").write_bytes(b"fixture-exe")
    (managed / "Assembly-CSharp.dll").write_bytes(b"fixture-assembly")
    manifest = steam_root / "steamapps" / "appmanifest_457140.acf"
    manifest.write_text('"appid" "457140"\n"buildid" "fixture-build"\n', encoding="utf-8")

    data_root = root / "Klei" / "OxygenNotIncluded"
    bridge = data_root / "ai-bridge"
    for queue in doctor.QUEUE_NAMES:
        (bridge / "commands" / queue).mkdir(parents=True, exist_ok=True)
    session = "session-doctor-fixture"
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    write_json(
        bridge / "health.json",
        {
            "schema": "oni.bridge.health.v1",
            "status": "ok",
            "current_session_id": session,
            "latest_seq": 3,
            "updated_at": timestamp,
        },
    )
    write_json(
        bridge / "latest_state.json",
        {
            "schema": "oni.state.v2",
            "session_id": session,
            "snapshot_id": f"{session}:3",
            "game_build": "cl-740622",
            "mod_version": "0.7.0",
            "seq": 3,
            "game": {"paused": True},
            "cycle": 1,
            "duplicants": {"total": 3},
            "resources": {"food_kcal": 10000},
            "research": {"current": ""},
            "bridge": {"stale": False},
            "quality": {
                "game.paused": {"status": "observed"},
                "cycle": {"status": "observed"},
                "duplicants.total": {"status": "observed"},
                "resources.food_kcal": {"status": "observed"},
                "research.current": {"status": "observed"},
            },
        },
    )
    write_json(
        bridge / "action-policy.json",
        {
            "schema": "oni.action-policy.v1",
            "actions_enabled": False,
            "suggestions_enabled": True,
            "allowed_actions": ["noop", "focus_note"],
            "allowed_save_hashes": [],
        },
    )
    write_json(
        bridge / "validation-control-policy.json",
        {
            "schema": "oni.validation-control-policy.v1",
            "controls_enabled": False,
            "allowed_operations": [],
        },
    )
    write_json(
        bridge / "llm-health.json",
        {
            "schema": "oni.llm-health.v1",
            "enabled": True,
            "called": True,
            "status": "ok",
            "api_key_present": True,
            "model": "fixture-model",
            "base_url": "https://api.openai.com/v1",
        },
    )

    player_log = root / "Player.log"
    player_log.write_text(
        "Build: U59-740622-SCR (release)\n"
        "Loading mod content DLL [ONI AI Probe:oni-ai-colony-agent]\n"
        "[ONI AI Probe] Loading observer-default probe V2\n",
        encoding="utf-8",
    )
    artifact = root / "OniAiProbeMod.dll"
    artifact.write_bytes(b"fixture-probe")
    installed = data_root / "mods" / "Dev" / doctor.MOD_ID / "OniAiProbeMod.dll"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(artifact.read_bytes())
    secrets = root / "secrets"
    secrets.mkdir()
    return bridge, oni_dir, player_log, secrets


def run() -> Dict[str, Any]:
    original = {
        "select_build_artifact": doctor.select_build_artifact,
        "source_mod_version": doctor.source_mod_version,
        "load_optional_manifest": doctor.load_optional_manifest,
        "find_process": doctor.find_process,
    }
    sentinel = "doctor-fixture-secret-never-print-this-value-123456"
    try:
        with tempfile.TemporaryDirectory(prefix="oni-doctor-") as name:
            root = pathlib.Path(name)
            bridge, oni_dir, player_log, secrets = healthy_fixture(root)
            (secrets / "key.env").write_text(f"OPENAI_API_KEY={sentinel}\n", encoding="utf-8")
            doctor.select_build_artifact = lambda: root / "OniAiProbeMod.dll"
            doctor.source_mod_version = lambda: "0.7.0"
            doctor.load_optional_manifest = lambda: None
            doctor.find_process = lambda _name: True

            player_log.write_text(
                player_log.read_text(encoding="utf-8")
                + "[ONI AI Probe] Patched Game.OnSpawn\n"
                + "[ONI AI Probe] Patched Game.OnCleanUp\n",
                encoding="utf-8",
            )
            parsed_patch = doctor.parse_player_log(player_log)
            require(parsed_patch["probe_patch_registered"] is True,
                    "paired Probe patch success markers were not recognized")
            player_log.write_text(
                player_log.read_text(encoding="utf-8").replace(
                    "[ONI AI Probe] Patched Game.OnCleanUp\n", ""),
                encoding="utf-8",
            )
            require(doctor.parse_player_log(player_log)["probe_patch_registered"] is False,
                    "a partial Probe patch marker was treated as complete")
            player_log.write_text(
                player_log.read_text(encoding="utf-8")
                + "[ONI AI Probe] Patched Game.OnCleanUp\n",
                encoding="utf-8",
            )

            require(
                doctor.builds_compatible("cl-740622", "U59-740622-SCR"),
                "equivalent changelist labels were not normalized",
            )
            require(
                not doctor.builds_compatible("cl-740622", "U59-740623-SCR"),
                "different changelist labels were treated as equivalent",
            )
            require(
                not doctor.builds_compatible("version-2026", "release-2026"),
                "unparseable labels with incidental digits were treated as equivalent",
            )

            report = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
            require(report["status"] == "healthy" and report["exit_code"] == 0, "healthy fixture did not pass")
            require(report["checks"]["llm"]["api_key_present"] is True, "key presence was not detected")
            require(sentinel not in encoded, "secret material leaked into the doctor report")
            require("Authorization" not in encoded, "authorization header leaked into the doctor report")
            require(
                not any(item.get("code") == "runtime_build_differs" for item in report["issues"]),
                "equivalent changelist build formats were reported as different",
            )

            latest_state_path = bridge / "latest_state.json"
            latest_state = json.loads(latest_state_path.read_text(encoding="utf-8"))
            latest_state["game_build"] = "cl-740623"
            write_json(latest_state_path, latest_state)
            mismatched = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(
                any(item.get("code") == "runtime_build_differs" for item in mismatched["issues"]),
                "different changelist builds were not reported",
            )
            latest_state["game_build"] = "cl-740622"
            write_json(latest_state_path, latest_state)

            approved = bridge / "commands" / "approved" / "fixture.json"
            write_json(approved, {"schema": "oni.command.v2", "action": "noop"})
            blocked = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(blocked["exit_code"] == 2 and blocked["status"] == "blocked", "approved queue was not blocked")
            require(any(item.get("code") == "approved_commands_present" for item in blocked["issues"]), "missing approved queue issue")
            blocked_encoded = json.dumps(blocked, ensure_ascii=False)
            require(sentinel not in blocked_encoded, "secret leaked in blocked report")
            approved.unlink()

            executing = bridge / "commands" / "executing" / "fixture-executing.json"
            write_json(
                executing,
                {
                    "schema": "oni.command.v3",
                    "id": "fixture-executing",
                    "status": "executing",
                    "action": "set_paused",
                    "attempt_id": "attempt-" + "1" * 32,
                },
            )
            executing_blocked = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(executing_blocked["exit_code"] == 2, "executing claim was not blocked")
            require(
                any(item.get("code") == "executing_commands_present" for item in executing_blocked["issues"]),
                "missing executing-claim issue",
            )
            executing.unlink()

            approving = bridge / "commands" / "approving" / "fixture-approving.json"
            write_json(approving, {"schema": "oni.command.v3", "id": "fixture-approving"})
            approving_blocked = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(approving_blocked["exit_code"] == 2, "approval claim was not blocked")
            require(
                any(item.get("code") == "approving_commands_present" for item in approving_blocked["issues"]),
                "missing approval-claim issue",
            )
            approving.unlink()

            confirming = bridge / "commands" / "confirming" / "fixture-confirming.json"
            write_json(confirming, {"schema": "oni.command.v3", "id": "fixture-confirming"})
            confirming_blocked = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(confirming_blocked["exit_code"] == 2, "confirming claim was not blocked")
            require(
                any(item.get("code") == "confirming_commands_present" for item in confirming_blocked["issues"]),
                "missing confirming-claim issue",
            )
            confirming.unlink()

            legacy_done = bridge / "commands" / "done" / "fixture-legacy.json"
            write_json(
                legacy_done,
                {
                    "schema": "oni.command.v3",
                    "id": "fixture-legacy",
                    "action": "set_paused",
                    "status": "verified",
                },
            )
            legacy_report = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(legacy_report["exit_code"] == 1, "legacy mutation was not classified read-only")
            require(
                any(item.get("code") == "legacy_mutating_done_read_only" for item in legacy_report["issues"]),
                "missing legacy read-only issue",
            )
            legacy_done.unlink()

            invalid_done = bridge / "commands" / "done" / "fixture-invalid-confirmed.json"
            write_json(
                invalid_done,
                {
                    "schema": "oni.command.v3",
                    "id": "fixture-invalid-confirmed",
                    "action": "set_paused",
                    "status": "confirmed",
                },
            )
            invalid_confirmed = doctor.run_doctor(
                bridge_dir=bridge,
                oni_dir=oni_dir,
                secrets_dir=secrets,
                player_log=player_log,
            )
            require(invalid_confirmed["exit_code"] == 2, "invalid confirmed mutation was not blocked")
            require(
                any(item.get("code") == "invalid_confirmed_mutation" for item in invalid_confirmed["issues"]),
                "missing invalid confirmed-mutation issue",
            )
            return {
                "healthy_exit": report["exit_code"],
                "mismatched_exit": mismatched["exit_code"],
                "blocked_exit": blocked["exit_code"],
                "executing_blocked_exit": executing_blocked["exit_code"],
                "approving_blocked_exit": approving_blocked["exit_code"],
                "confirming_blocked_exit": confirming_blocked["exit_code"],
                "legacy_read_only_exit": legacy_report["exit_code"],
                "invalid_confirmed_exit": invalid_confirmed["exit_code"],
                "key_source": report["checks"]["llm"]["source"],
            }
    finally:
        doctor.select_build_artifact = original["select_build_artifact"]
        doctor.source_mod_version = original["source_mod_version"]
        doctor.load_optional_manifest = original["load_optional_manifest"]
        doctor.find_process = original["find_process"]


if __name__ == "__main__":
    details = run()
    print(json.dumps({"schema": "oni.doctor-test.v1", "status": "passed", "details": details}, indent=2))
