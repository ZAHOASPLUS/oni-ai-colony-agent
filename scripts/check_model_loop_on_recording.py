"""Run the model loop offline on a recorded session (review 2026-09-26, M1).

Reads a session archive (``sessions/<id>/state.jsonl`` from an evidence
bundle; the archive is only read), keeps its start events and last snapshots,
renumbers them into one contiguous session and moves every timestamp by the
same whole-second offset so the newest snapshot is current.  The real Bridge
generation then runs with the model loop and a stand-in model that picks a
candidate (the first non-wait one, or ``--pick``), in a temporary Bridge
directory with a policy that allows the candidate actions for the recorded
save.  The published command is approved with the real receipt issuer and
approval validator and, with ``--executor-check``, checked by the executor's
own condition-shape and receipt validation (tools/ActionExecutorHarness).

No game, network or live Bridge directory is involved.  Example:

    py -3 scripts/check_model_loop_on_recording.py <bundle>/sessions/<session>/state.jsonl --executor-check
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import approval_receipt  # noqa: E402
from agent.command_queue import load_policy  # noqa: E402
from agent.model_loop import CHOICE_SCHEMA, WAIT_CANDIDATE_ID, ModelLoop  # noqa: E402
from bridge import oni_bridge  # noqa: E402
from scripts import commit_command_approval  # noqa: E402

HARNESS = ROOT / "tools" / "ActionExecutorHarness"
CANDIDATE_ACTIONS = ["noop", "focus_note", "set_paused", "set_duplicant_chore_priority", "mark_dig_cells"]
SMALL_RECORD_BYTES = 65536
_TIMESTAMP = re.compile(r'"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})"')


def load_recording(archive: pathlib.Path, keep: int) -> List[Dict[str, Any]]:
    """Return the start events and the last ``keep`` snapshots, renumbered and moved to now."""

    prefix: List[Dict[str, Any]] = []
    snapshots: List[bytes] = []
    with archive.open("rb") as stream:
        for raw in stream:
            if len(raw) < SMALL_RECORD_BYTES:
                record = json.loads(raw.decode("utf-8-sig"))
                if record.get("event") in ("session_started", "save_loaded") and not snapshots:
                    prefix.append(record)
                continue
            if b'"state_snapshot"' in raw:
                snapshots = (snapshots + [raw])[-keep:]
    if not prefix or prefix[0].get("event") != "session_started" or not snapshots:
        raise SystemExit("the archive must start with session_started and contain snapshots")
    records = prefix + [json.loads(raw.decode("utf-8-sig")) for raw in snapshots]
    if any(record.get("event") != "state_snapshot" for record in records[len(prefix):]):
        raise SystemExit("a large record is not a state snapshot")

    newest = dt.datetime.fromisoformat(records[-1]["ts"].replace("Z", "+00:00"))
    offset = dt.timedelta(seconds=round((dt.datetime.now(dt.timezone.utc) - newest).total_seconds()) - 1)

    def move(match: "re.Match[str]") -> str:
        base, fraction, zone = match.group(1), match.group(2) or "", match.group(3)
        moved = dt.datetime.fromisoformat(base + ("+00:00" if zone == "Z" else zone)) + offset
        return '"' + moved.strftime("%Y-%m-%dT%H:%M:%S") + fraction + zone + '"'

    # One whole-second offset for every capture timestamp keeps the freshness
    # relations inside each snapshot (reader captured_at versus ts) intact.
    records = [json.loads(_TIMESTAMP.sub(move, json.dumps(record))) for record in records]
    session = records[0]["session_id"]
    for index, record in enumerate(records):
        record["seq"] = index + 1
        if record.get("event") == "state_snapshot":
            record["snapshot_seq"] = index + 1 - len(prefix)
            record["snapshot_id"] = f"{session}:{index + 1}"
    return records


class StandInModel:
    def __init__(self, pick: Optional[str]) -> None:
        self.pick = pick
        self.prompts: List[str] = []

    def __call__(self, config, prompt_text, response_schema, *, timeout):
        self.prompts.append(prompt_text)
        ids = response_schema["properties"]["candidate_id"]["enum"]
        choice = self.pick if self.pick in ids else next(item for item in ids if item != WAIT_CANDIDATE_ID)
        content = json.dumps({"schema": CHOICE_SCHEMA, "candidate_id": choice, "reason": "offline recording check"})
        return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def executor_check(command: Dict[str, Any], policy: Dict[str, Any], temp: pathlib.Path) -> Dict[str, Any]:
    build = subprocess.run(["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
                           capture_output=True, text=True, errors="replace")
    if build.returncode != 0:
        return {"error": "harness_build_failed", "output": (build.stdout + build.stderr)[-1000:]}
    cases = temp / "cases.json"
    cases.write_text(json.dumps({"cases": [{"name": "recorded", "command": command, "policy": policy}]},
                                ensure_ascii=False), encoding="utf-8")
    run = subprocess.run(["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "ActionExecutorHarness.dll"),
                          str(cases)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if run.returncode != 0:
        return {"error": "harness_failed", "output": run.stderr[-1000:]}
    return json.loads(run.stdout)["results"][0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("archive", type=pathlib.Path, help="sessions/<id>/state.jsonl of a recorded session")
    parser.add_argument("--keep", type=int, default=12, help="snapshots to keep from the end (default 12)")
    parser.add_argument("--pick", help="candidate id for the stand-in model (default: first non-wait)")
    parser.add_argument("--executor-check", action="store_true", help="also run tools/ActionExecutorHarness")
    args = parser.parse_args()
    records = load_recording(args.archive, max(1, args.keep))
    report: Dict[str, Any] = {
        "session_id": records[0]["session_id"],
        "snapshots": sum(1 for record in records if record.get("event") == "state_snapshot"),
    }
    with tempfile.TemporaryDirectory(prefix="oni-model-loop-recording-") as name:
        temp = pathlib.Path(name)
        bridge = temp / "bridge"
        bridge.mkdir()
        policy = {
            "schema": "oni.action-policy.v1", "actions_enabled": True, "suggestions_enabled": True,
            "allowed_actions": CANDIDATE_ACTIONS, "allowed_save_hashes": [records[-1].get("save_id_hash")],
            "command_ttl_seconds": 300, "max_pending_commands": 20,
        }
        (bridge / "action-policy.json").write_text(json.dumps(policy), encoding="utf-8")
        with (bridge / "state.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        runtime, _ = oni_bridge.replay_initial_records(bridge / "state.jsonl", bridge)
        model = StandInModel(args.pick)
        loop = ModelLoop(model_caller=model, min_interval_seconds=0, config_override={
            "api_key": "sk-offline-recording-check-0000", "base_url": "https://model.invalid/v1", "model": "stand-in"})

        def generation() -> Dict[str, Any]:
            oni_bridge.write_outputs(bridge, runtime, model_loop=loop)
            return json.loads((bridge / "model-loop.json").read_text(encoding="utf-8"))

        first = generation()
        health = json.loads((bridge / "health.json").read_text(encoding="utf-8"))
        advice = json.loads((bridge / "advice.json").read_text(encoding="utf-8"))
        report.update({
            "trust_ready": health.get("trust_ready"), "trust_blockers": health.get("trust_blockers"),
            "rule_findings": [(item.get("incident_id"), (item.get("command") or {}).get("action"))
                              for item in advice.get("items", [])],
            "loop_blockers": first.get("blockers"),
            "candidates": [(item["candidate_id"], item.get("summary")) for item in first.get("candidates") or []],
        })
        if first.get("phase") == "awaiting_model":
            loop.join_request(30)
            second = generation()
            report["published"] = second.get("phase") == "awaiting_human_approval"
            pending = sorted((bridge / "commands" / "pending").glob("*.json"))
            if pending:
                command = json.loads(pending[0].read_text(encoding="utf-8"))
                digest = command["source_frame_id"].split(":", 1)[1]
                frame_path = bridge / "decision-frames" / f"sha256-{digest}.json"
                frame = json.loads(frame_path.read_text(encoding="utf-8"))
                policy_now = load_policy(bridge)
                approved = approval_receipt.build_approved_command(
                    command, policy_now, frame, approved_by="local_human",
                    approved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                )
                approval_receipt.validate_approval_receipt(approved, policy_now, frame)
                commit_command_approval._validate_payloads(command, approved, bridge / "action-policy.json", frame_path)
                report["command"] = {"action": command["action"], "args": command["args"], "approval": "validated"}
                if args.executor_check:
                    report["executor_check"] = executor_check(approved, policy_now, temp)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = report.get("command", {}).get("approval") == "validated"
    if args.executor_check:
        check = report.get("executor_check") or {}
        ok = ok and check.get("shape_error") is None and check.get("receipt_error") is None and "error" not in check
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
