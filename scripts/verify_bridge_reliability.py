#!/usr/bin/env python3
"""Fault-injection and budget checks for the ONI Bridge V2."""

from __future__ import annotations

import argparse
import copy
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import state_quality
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_COMPLETE_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    contract_quality_fixture,
)
from bridge import oni_bridge
from agent.control_packet import ControlPacketError, validate_control_packet
from agent.decision import validate_decision_frame
from agent.decision_frame_archive import decision_frame_archive_path


PERF_LINE_COUNT = 10_000
MAX_REPLAY_SECONDS = 5.0
MAX_PEAK_WORKING_SET = 150 * 1024 * 1024
MAX_LIVE_INITIAL_REPLAY_SECONDS = 8.0
ASSERTIONS = 0


def iso_now(offset_seconds: int = 0) -> str:
    value = dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(seconds=offset_seconds)
    return value.isoformat(timespec="milliseconds")


def make_state(
    session_id: str,
    seq: int,
    *,
    cycle: int = 10,
    timestamp: str | None = None,
    snapshot_seq: int | None = None,
) -> Dict[str, Any]:
    snapshot_ts = timestamp or iso_now()
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": snapshot_ts,
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": seq if snapshot_seq is None else snapshot_seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "cl-test",
        "mod_version": "0.2.0-test",
        "save_id_hash": "sha256:" + "a" * 64,
        "save_dirty": False,
        "world_id": 0,
        "observation_scope": {
            "active_world_id": 0,
            "populated_world_ids": [0],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": True,
        },
        "cycle": cycle,
        "game": {"paused": False},
        "duplicants": {"total": 3, "stressed": 0, "idle": 0},
        "resources": {
            "food_kcal": 100_000.0,
            "oxygen_kg_estimate": 100.0,
            "water_kg_estimate": 1_000.0,
        },
        "alerts": [],
        "research": {"current": "FarmingTech", "complete": []},
        "buildings": {
            "oxygen_diffuser": 1,
            "outhouse": 1,
            "manual_generator": 1,
            "captured_at": snapshot_ts,
            "_scanned": 3,
            "_source": "test_fixture",
            "_sample_ids": ["OxygenDiffuser", "Outhouse", "ManualGenerator"],
            "_top_prefabs": [],
        },
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            "cycle": {"status": "observed", "source": "fixture"},
            "duplicants.total": contract_quality_fixture(DUPLICANTS_TOTAL_CONTRACT_ID),
            "duplicants.stressed": contract_quality_fixture(DUPLICANTS_STRESSED_CONTRACT_ID),
            "duplicants.idle": contract_quality_fixture(DUPLICANTS_IDLE_CONTRACT_ID),
            "resources.food_kcal": contract_quality_fixture(REGISTERED_FOOD_CONTRACT_ID),
            "resources.oxygen_kg_estimate": {"status": "observed", "source": "fixture"},
            "resources.water_kg_estimate": {"status": "observed", "source": "fixture"},
            "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
            "research.current": contract_quality_fixture(RESEARCH_CURRENT_CONTRACT_ID),
            "research.complete": contract_quality_fixture(RESEARCH_COMPLETE_CONTRACT_ID),
            "buildings.oxygen_diffuser": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
            "buildings.outhouse": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
            "buildings.manual_generator": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
            "buildings._source": {"status": "observed", "source": "fixture"},
            "buildings._scanned": {"status": "observed", "source": "fixture"},
        },
        "sample": {"interval_seconds": 5.0, "duration_ms": 1.0},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
        "read_metrics_ms": {"game": 0.1, "resources": 0.2},
    }


def json_line(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def startup_expectations() -> Dict[str, Any]:
    return {
        "validation_run_id": "oni-live-startup-test",
        "validation_process_id": 4242,
        "startup_binding_hash": "sha256:" + "1" * 64,
        "startup_intent_hash": "sha256:" + "2" * 64,
        "runtime_config_evidence_sha256": "sha256:" + "3" * 64,
        "loader_path_id_upper": "sha256:" + "4" * 64,
        "loader_save_sha256": "sha256:" + "5" * 64,
        "game_build": "cl-test",
        "mod_version": "0.2.0-test",
    }


def attach_startup_contract(
    record: Dict[str, Any],
    expected: Dict[str, Any],
    *,
    process_id: int = 4242,
) -> Dict[str, Any]:
    value = copy.deepcopy(record)
    value.update(
        {
            "validation_run_id": expected["validation_run_id"],
            "startup_binding_hash": expected["startup_binding_hash"],
            "startup_intent_hash": expected["startup_intent_hash"],
            "runtime_config_evidence_sha256": expected["runtime_config_evidence_sha256"],
            "validation_process_id": process_id,
            "startup_binding_status": "verified",
            "requested_save_path_id": expected["loader_path_id_upper"],
            "active_save_path_id": expected["loader_path_id_upper"],
        }
    )
    return value


def startup_event(
    expected: Dict[str, Any],
    session_id: str,
    seq: int,
    event: str,
    *,
    active_path_id: str,
    save_id_hash: str,
    requested_path_id: str | None = None,
    **fields: Any,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema": "oni.event.v2",
        "event": event,
        "ts": iso_now(),
        "session_id": session_id,
        "seq": seq,
        "game_build": expected["game_build"],
        "mod_version": expected["mod_version"],
        "save_id_hash": save_id_hash,
    }
    record.update(fields)
    value = attach_startup_contract(record, expected)
    value["requested_save_path_id"] = (
        expected["loader_path_id_upper"]
        if requested_path_id is None
        else requested_path_id
    )
    value["active_save_path_id"] = active_path_id
    return value


def startup_snapshot(
    expected: Dict[str, Any],
    session_id: str,
    seq: int,
    snapshot_seq: int,
    *,
    active_path_id: str,
    save_id_hash: str,
    paused: bool = True,
) -> Dict[str, Any]:
    value = attach_startup_contract(
        make_state(session_id, seq, snapshot_seq=snapshot_seq),
        expected,
    )
    value["active_save_path_id"] = active_path_id
    value["save_id_hash"] = save_id_hash
    value["game"]["paused"] = paused
    return value


def startup_save_checkpoint(
    expected: Dict[str, Any],
    session_id: str,
    seq: int,
    *,
    control_id: str,
    active_before: str,
    target_path_id: str,
    save_before: str,
    save_after: str,
) -> List[Dict[str, Any]]:
    return [
        startup_event(
            expected,
            session_id,
            seq,
            "validation_control_started",
            active_path_id=active_before,
            save_id_hash=save_before,
            control_id=control_id,
            operation="save_checkpoint",
            test_run_id=expected["validation_run_id"],
            target_save_path_id=target_path_id,
        ),
        startup_event(
            expected,
            session_id,
            seq + 1,
            "save_write_started",
            active_path_id=active_before,
            save_id_hash=save_before,
            requested_path_id=target_path_id,
            is_auto_save=False,
            update_save_pointer=True,
            before_save_id_hash=save_before,
        ),
        startup_event(
            expected,
            session_id,
            seq + 2,
            "save_write_completed",
            active_path_id=target_path_id,
            save_id_hash=save_after,
            requested_path_id=target_path_id,
            success=True,
            is_auto_save=False,
            update_save_pointer=True,
            file_rewrite_observed=True,
            save_file_readable=True,
            failure_reason=None,
            result_save_path_id=target_path_id,
            before_save_id_hash=save_before,
            written_save_id_hash=save_after,
            active_save_id_hash=save_after,
        ),
    ]


def startup_reload_boundary(
    expected: Dict[str, Any],
    source_session_id: str,
    seq: int,
    target_session_id: str,
    *,
    active_path_id: str,
    save_id_hash: str,
) -> List[Dict[str, Any]]:
    return [
        startup_event(
            expected,
            source_session_id,
            seq,
            "validation_control_started",
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
            control_id=f"reload-{source_session_id}",
            operation="reload_checkpoint",
            test_run_id=expected["validation_run_id"],
            expected_save_path_id=active_path_id,
            expected_save_id_hash=save_id_hash,
        ),
        startup_event(
            expected,
            source_session_id,
            seq + 1,
            "save_unloaded",
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
            reason="save_loader_load_start",
        ),
        startup_event(
            expected,
            source_session_id,
            seq + 2,
            "session_ended",
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
            reason="save_loader_load_start",
        ),
        startup_event(
            expected,
            target_session_id,
            1,
            "session_started",
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
            reason="save_loader_load_complete",
        ),
        startup_event(
            expected,
            target_session_id,
            2,
            "save_loaded",
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
            reason="save_loader_load_complete",
        ),
        startup_snapshot(
            expected,
            target_session_id,
            3,
            1,
            active_path_id=active_path_id,
            save_id_hash=save_id_hash,
        ),
    ]


def authorized_derivative_chain(
    expected: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    loader_path = expected["loader_path_id_upper"]
    loader_save = expected["loader_save_sha256"]
    derivative_path = "sha256:" + "d" * 64
    derivative_save = "sha256:" + "6" * 64
    forward_save = "sha256:" + "7" * 64
    inverse_save = "sha256:" + "8" * 64
    sessions = ("origin-s0", "activation-s1", "forward-s2", "inverse-s3")

    records: List[Dict[str, Any]] = [
        startup_event(
            expected,
            sessions[0],
            1,
            "session_started",
            active_path_id=loader_path,
            save_id_hash=loader_save,
            reason="save_loader_load_complete",
        ),
        startup_event(
            expected,
            sessions[0],
            2,
            "save_loaded",
            active_path_id=loader_path,
            save_id_hash=loader_save,
            reason="save_loader_load_complete",
        ),
        startup_snapshot(
            expected,
            sessions[0],
            3,
            1,
            active_path_id=loader_path,
            save_id_hash=loader_save,
        ),
    ]
    records.extend(
        startup_save_checkpoint(
            expected,
            sessions[0],
            4,
            control_id="origin-save-as",
            active_before=loader_path,
            target_path_id=derivative_path,
            save_before=loader_save,
            save_after=derivative_save,
        )
    )
    records.append(
        startup_snapshot(
            expected,
            sessions[0],
            7,
            2,
            active_path_id=derivative_path,
            save_id_hash=derivative_save,
        )
    )
    records.extend(
        startup_reload_boundary(
            expected,
            sessions[0],
            8,
            sessions[1],
            active_path_id=derivative_path,
            save_id_hash=derivative_save,
        )
    )
    records.extend(
        startup_save_checkpoint(
            expected,
            sessions[1],
            4,
            control_id="forward-save",
            active_before=derivative_path,
            target_path_id=derivative_path,
            save_before=derivative_save,
            save_after=forward_save,
        )
    )
    records.append(
        startup_snapshot(
            expected,
            sessions[1],
            7,
            2,
            active_path_id=derivative_path,
            save_id_hash=forward_save,
        )
    )
    records.extend(
        startup_reload_boundary(
            expected,
            sessions[1],
            8,
            sessions[2],
            active_path_id=derivative_path,
            save_id_hash=forward_save,
        )
    )
    records.extend(
        startup_save_checkpoint(
            expected,
            sessions[2],
            4,
            control_id="inverse-save",
            active_before=derivative_path,
            target_path_id=derivative_path,
            save_before=forward_save,
            save_after=inverse_save,
        )
    )
    records.append(
        startup_snapshot(
            expected,
            sessions[2],
            7,
            2,
            active_path_id=derivative_path,
            save_id_hash=inverse_save,
        )
    )
    records.extend(
        startup_reload_boundary(
            expected,
            sessions[2],
            8,
            sessions[3],
            active_path_id=derivative_path,
            save_id_hash=inverse_save,
        )
    )
    return records, {
        "loader_path": loader_path,
        "loader_save": loader_save,
        "derivative_path": derivative_path,
        "derivative_save": derivative_save,
        "forward_save": forward_save,
        "inverse_save": inverse_save,
        "origin_session": sessions[0],
        "activation_session": sessions[1],
        "forward_session": sessions[2],
        "final_session": sessions[3],
    }


def require(condition: bool, message: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def synthetic_winerror(code: int) -> OSError:
    error = OSError(errno.EINVAL, f"synthetic WinError {code} destination lock")
    error.winerror = code
    return error


def case_atomic_replace_retries(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True)
    original_replace = pathlib.Path.replace

    target = root / "ordinary" / "plan.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"status": "old"}\n', encoding="utf-8")
    ordinary_calls = 0

    def flaky_ordinary_replace(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
        nonlocal ordinary_calls
        ordinary_calls += 1
        if ordinary_calls <= 2:
            raise synthetic_winerror(5)
        return original_replace(source, destination)

    with (
        mock.patch.object(pathlib.Path, "replace", new=flaky_ordinary_replace),
        mock.patch.object(oni_bridge.time, "sleep") as ordinary_sleep,
    ):
        oni_bridge.atomic_write_json(target, {"status": "new"})

    require(json.loads(target.read_text(encoding="utf-8")) == {"status": "new"}, "retry did not replace output")
    require(ordinary_calls == 3, "WinError 5 was not retried twice before success")
    require(
        [call.args[0] for call in ordinary_sleep.call_args_list]
        == [
            oni_bridge._ATOMIC_REPLACE_INITIAL_DELAY_SECONDS,
            oni_bridge._ATOMIC_REPLACE_INITIAL_DELAY_SECONDS * 2,
        ],
        "ordinary output retry delays are wrong",
    )

    archive_root = root / "archive"
    archive_target = archive_root / "sessions" / "session-atomic" / "state.jsonl"
    archive_target.parent.mkdir(parents=True)
    archive_target.write_text('{"seq":0}\n', encoding="utf-8")
    archive = oni_bridge.SessionArchiveBuilder(archive_root)
    archive.add({"schema": "oni.state.v2", "session_id": "session-atomic", "seq": 1})
    archive_calls = 0

    def flaky_archive_replace(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise synthetic_winerror(32)
        return original_replace(source, destination)

    with (
        mock.patch.object(pathlib.Path, "replace", new=flaky_archive_replace),
        mock.patch.object(oni_bridge.time, "sleep") as archive_sleep,
    ):
        archive.commit()

    archived = [json.loads(line) for line in archive_target.read_text(encoding="utf-8").splitlines()]
    require([item["seq"] for item in archived] == [0, 1], "session archive merge lost existing history")
    require(archive_calls == 2 and archive_sleep.call_count == 1, "WinError 32 archive retry is wrong")

    restart_archive = oni_bridge.SessionArchiveBuilder(archive_root)
    restart_archive.add({"schema": "oni.state.v2", "session_id": "session-atomic", "seq": 1})
    restart_archive.add({"schema": "oni.state.v2", "session_id": "session-atomic", "seq": 2})
    restart_archive.commit()
    restarted = [json.loads(line) for line in archive_target.read_text(encoding="utf-8").splitlines()]
    require(
        [item["seq"] for item in restarted] == [0, 1, 2],
        "session restart duplicated the tail or truncated prior evidence",
    )

    persistent = root / "persistent" / "plan.json"
    persistent.parent.mkdir(parents=True)
    persistent.write_text('{"status": "old"}\n', encoding="utf-8")
    with (
        mock.patch.object(pathlib.Path, "replace", side_effect=synthetic_winerror(33)) as replace,
        mock.patch.object(oni_bridge.time, "sleep") as persistent_sleep,
    ):
        try:
            oni_bridge.atomic_write_json(persistent, {"status": "blocked"})
        except OSError as exc:
            require(getattr(exc, "winerror", None) == 33, "persistent lock raised the wrong error")
        else:
            raise AssertionError("persistent WinError 33 unexpectedly succeeded")

    require(replace.call_count == oni_bridge._ATOMIC_REPLACE_ATTEMPTS, "persistent retry count exceeded its budget")
    require(
        persistent_sleep.call_count == oni_bridge._ATOMIC_REPLACE_ATTEMPTS - 1,
        "persistent retry sleep count exceeded its budget",
    )
    require(json.loads(persistent.read_text(encoding="utf-8")) == {"status": "old"}, "failed replace damaged target")

    non_retryable = root / "non-retryable" / "plan.json"
    non_retryable.parent.mkdir(parents=True)
    non_retryable.write_text('{"status": "old"}\n', encoding="utf-8")
    with (
        mock.patch.object(
            pathlib.Path,
            "replace",
            side_effect=FileNotFoundError(errno.ENOENT, "synthetic missing source"),
        ) as non_retryable_replace,
        mock.patch.object(oni_bridge.time, "sleep") as non_retryable_sleep,
    ):
        try:
            oni_bridge.atomic_write_json(non_retryable, {"status": "missing"})
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("non-retryable replace error unexpectedly succeeded")

    require(non_retryable_replace.call_count == 1, "non-retryable error was retried")
    require(not non_retryable_sleep.called, "non-retryable error slept before failing")
    require(json.loads(non_retryable.read_text(encoding="utf-8")) == {"status": "old"}, "fast failure damaged target")
    require(not list(root.rglob("*.tmp")), "atomic replace regression left temporary files")
    return {
        "ordinary_replace_calls": ordinary_calls,
        "archive_replace_calls": archive_calls,
        "persistent_replace_calls": replace.call_count,
        "non_retryable_replace_calls": non_retryable_replace.call_count,
    }


def case_archive_identity_noop_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True)
    session_id = "session-archive-identity"
    first = {"schema": "oni.state.v2", "session_id": session_id, "seq": 1}
    second = {"schema": "oni.state.v2", "session_id": session_id, "seq": 2}

    def canonical_bytes(*records: Dict[str, Any]) -> bytes:
        return b"".join(oni_bridge.SessionArchiveBuilder._canonical_line(record).encode("utf-8") + b"\n" for record in records)

    original_replace = pathlib.Path.replace

    def exercise(
        name: str,
        existing: bytes,
        incoming: List[Dict[str, Any]],
        *,
        preexisting_temporary: bool = False,
        compare_hook: Callable[[pathlib.Path, pathlib.Path, tuple[int, ...]], bool] | None = None,
        hard_link: bool = False,
        max_record_bytes: int | None = None,
        external_temporary_names: tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        archive_root = root / name
        target = archive_root / "sessions" / session_id / "state.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(existing)
        linked = target.with_name("state-hardlink.jsonl")
        if hard_link:
            os.link(target, linked)
        rogue_temporary = target.parent / "interrupted.tmp"
        if preexisting_temporary:
            rogue_temporary.write_bytes(b"stale")

        builder = oni_bridge.SessionArchiveBuilder(archive_root)
        for record in incoming:
            builder.add(copy.deepcopy(record))

        replace_calls = 0

        def observe_replace(temporary: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
            nonlocal replace_calls
            replace_calls += 1
            return original_replace(temporary, destination)

        compare_patch = (
            mock.patch.object(
                oni_bridge.SessionArchiveBuilder,
                "_files_have_identical_bytes",
                new=staticmethod(compare_hook),
            )
            if compare_hook is not None
            else mock.patch.object(
                oni_bridge.SessionArchiveBuilder,
                "_files_have_identical_bytes",
                wraps=oni_bridge.SessionArchiveBuilder._files_have_identical_bytes,
            )
        )
        record_limit_patch = (
            mock.patch.object(oni_bridge, "MAX_EVIDENCE_RECORD_BYTES", max_record_bytes)
            if max_record_bytes is not None
            else mock.patch.object(
                oni_bridge,
                "MAX_EVIDENCE_RECORD_BYTES",
                oni_bridge.MAX_EVIDENCE_RECORD_BYTES,
            )
        )
        with (
            mock.patch.object(pathlib.Path, "replace", new=observe_replace),
            compare_patch,
            record_limit_patch,
        ):
            builder.commit()

        require(replace_calls == 1, f"{name} incorrectly took the archive identity no-op")
        require(not builder.temporaries, f"{name} retained a builder temporary")
        if preexisting_temporary:
            rogue_temporary.unlink()
        for temporary_name in external_temporary_names:
            external_temporary = target.parent / temporary_name
            require(external_temporary.is_file(), f"{name} did not create its temporary anomaly")
            external_temporary.unlink()
        require(not list(target.parent.glob("*.tmp")), f"{name} left an archive temporary")
        return {
            "replace_calls": replace_calls,
            "target_bytes": target.stat().st_size,
            "hard_link_guarded": hard_link,
        }

    complete_root = root / "complete"
    complete_target = complete_root / "sessions" / session_id / "state.jsonl"
    complete_target.parent.mkdir(parents=True)
    complete_target.write_bytes(canonical_bytes(first, second))
    complete_before = complete_target.stat()
    complete_identity = (int(complete_before.st_dev), int(complete_before.st_ino))
    complete_raw = complete_target.read_bytes()
    complete_sha256 = hashlib.sha256(complete_raw).hexdigest()
    complete_builder = oni_bridge.SessionArchiveBuilder(complete_root)
    complete_builder.add(copy.deepcopy(first))
    complete_builder.add(copy.deepcopy(second))
    complete_replace_calls = 0

    def observe_complete_replace(temporary: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
        nonlocal complete_replace_calls
        complete_replace_calls += 1
        return original_replace(temporary, destination)

    with mock.patch.object(pathlib.Path, "replace", new=observe_complete_replace):
        complete_builder.commit()
    complete_after = complete_target.stat()
    require(complete_replace_calls == 0, "complete canonical archive was atomically replaced")
    require(
        (int(complete_after.st_dev), int(complete_after.st_ino)) == complete_identity,
        "complete canonical archive identity changed",
    )
    require(complete_target.read_bytes() == complete_raw, "complete canonical archive bytes changed")
    require(
        hashlib.sha256(complete_target.read_bytes()).hexdigest() == complete_sha256,
        "complete canonical archive SHA-256 changed",
    )
    require(not list(complete_root.rglob("*.tmp")), "complete canonical no-op left a temporary")

    negatives: Dict[str, Dict[str, Any]] = {}
    negatives["missing_line"] = exercise(
        "missing-line",
        canonical_bytes(first),
        [first, second],
    )
    changed = copy.deepcopy(first)
    changed["save_id_hash"] = "sha256:" + "f" * 64
    negatives["content_difference"] = exercise(
        "content-difference",
        canonical_bytes(changed),
        [first],
    )
    second_bytes = canonical_bytes(second)
    negatives["truncated_tail"] = exercise(
        "truncated-tail",
        canonical_bytes(first) + second_bytes[: max(1, len(second_bytes) // 2)],
        [first, second],
    )
    noncanonical = json.dumps(first, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    negatives["noncanonical_json"] = exercise(
        "noncanonical-json",
        noncanonical,
        [first],
    )
    negatives["utf8_bom"] = exercise(
        "utf8-bom",
        b"\xef\xbb\xbf" + canonical_bytes(first),
        [first],
    )
    negatives["crlf"] = exercise(
        "crlf",
        canonical_bytes(first).replace(b"\n", b"\r\n"),
        [first],
    )
    negatives["duplicate"] = exercise(
        "duplicate",
        canonical_bytes(first, first),
        [first],
    )
    negatives["preexisting_tmp"] = exercise(
        "preexisting-tmp",
        canonical_bytes(first),
        [first],
        preexisting_temporary=True,
    )
    oversized = copy.deepcopy(first)
    oversized["payload"] = "x" * 512
    negatives["oversized"] = exercise(
        "oversized",
        canonical_bytes(oversized),
        [oversized],
        max_record_bytes=128,
    )
    negatives["hard_link"] = exercise(
        "hard-link",
        canonical_bytes(first),
        [first],
        hard_link=True,
    )

    original_compare = oni_bridge.SessionArchiveBuilder._files_have_identical_bytes

    def comparison_error(
        candidate: pathlib.Path,
        target: pathlib.Path,
        expected_version: tuple[int, ...],
    ) -> bool:
        raise OSError(errno.EIO, "synthetic archive comparison failure")

    negatives["comparison_error"] = exercise(
        "comparison-error",
        canonical_bytes(first),
        [first],
        compare_hook=comparison_error,
    )

    def exchange_name(
        candidate: pathlib.Path,
        target: pathlib.Path,
        expected_version: tuple[int, ...],
    ) -> bool:
        replacement = target.with_name("same-bytes-replacement.jsonl")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
        return original_compare(candidate, target, expected_version)  # type: ignore[arg-type]

    negatives["name_exchange"] = exercise(
        "name-exchange",
        canonical_bytes(first),
        [first],
        compare_hook=exchange_name,
    )

    def concurrent_write(
        candidate: pathlib.Path,
        target: pathlib.Path,
        expected_version: tuple[int, ...],
    ) -> bool:
        with target.open("r+b") as handle:
            handle.seek(0)
            handle.write(b"!")
            handle.flush()
            os.fsync(handle.fileno())
        return original_compare(candidate, target, expected_version)  # type: ignore[arg-type]

    negatives["concurrent_write"] = exercise(
        "concurrent-write",
        canonical_bytes(first),
        [first],
        compare_hook=concurrent_write,
    )

    def concurrent_temporary(
        candidate: pathlib.Path,
        target: pathlib.Path,
        expected_version: tuple[int, ...],
    ) -> bool:
        (target.parent / "late-interrupted.tmp").write_bytes(b"late")
        return original_compare(candidate, target, expected_version)  # type: ignore[arg-type]

    negatives["concurrent_tmp"] = exercise(
        "concurrent-tmp",
        canonical_bytes(first),
        [first],
        compare_hook=concurrent_temporary,
        external_temporary_names=("late-interrupted.tmp",),
    )

    reparse_root = root / "reparse"
    physical_parent = root / "reparse-physical"
    physical_parent.mkdir()
    archive_root = reparse_root / "archive"
    sessions_root = archive_root / "sessions"
    sessions_root.mkdir(parents=True)
    reparse_parent = sessions_root / session_id
    if os.name == "nt":
        junction = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(reparse_parent),
                str(physical_parent),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        require(junction.returncode == 0, f"junction fixture failed: {junction.stderr.strip()}")
    else:
        reparse_parent.symlink_to(physical_parent, target_is_directory=True)
    try:
        reparse_target = reparse_parent / "state.jsonl"
        reparse_target.write_bytes(canonical_bytes(first))
        reparse_builder = oni_bridge.SessionArchiveBuilder(archive_root)
        reparse_builder.add(copy.deepcopy(first))
        reparse_replace_calls = 0

        def observe_reparse_replace(
            temporary: pathlib.Path,
            destination: pathlib.Path,
        ) -> pathlib.Path:
            nonlocal reparse_replace_calls
            reparse_replace_calls += 1
            return original_replace(temporary, destination)

        with mock.patch.object(pathlib.Path, "replace", new=observe_reparse_replace):
            reparse_builder.commit()
        require(reparse_replace_calls == 1, "reparse-point archive incorrectly took identity no-op")
        require(not reparse_builder.temporaries, "reparse-point archive retained a builder temporary")
        negatives["reparse_point"] = {"replace_calls": reparse_replace_calls}
    finally:
        reparse_parent.rmdir()

    return {
        "complete_replace_calls": complete_replace_calls,
        "complete_identity_preserved": True,
        "complete_sha256": complete_sha256,
        "negative_count": len(negatives),
        "negative_results": negatives,
    }


def case_partial_bad_and_truncated(root: pathlib.Path) -> Dict[str, Any]:
    log = root / "partial" / "state.jsonl"
    log.parent.mkdir(parents=True)
    first = json_line(make_state("session-partial", 1))
    second = json_line(make_state("session-partial", 2, cycle=11))
    midpoint = len(second) // 2
    log.write_bytes(first + b"\n" + second[:midpoint])

    initial, position, invalid = oni_bridge.load_recent_records(log)
    require(len(initial) == 1 and invalid == 0, "initial reader consumed an incomplete line")
    new_records, unchanged_position, _, partial = oni_bridge.read_new_records(log, position)
    require(not new_records and unchanged_position == position, "partial line advanced the cursor")
    require(partial == midpoint, "partial byte count is wrong")

    with log.open("ab") as handle:
        handle.write(second[midpoint:] + b"\n")
    completed, position, invalid, partial = oni_bridge.read_new_records(log, position)
    require(len(completed) == 1 and completed[0]["seq"] == 2, "completed partial line was not recovered")
    require(invalid == 0 and partial == 0, "completed line retained an invalid/partial flag")

    old_position = position
    replacement = json_line(make_state("session-after-truncate", 1, cycle=20)) + b"\n"
    require(len(replacement) < old_position, "test fixture did not create a real truncation")
    log.write_bytes(replacement)
    truncated, position, invalid, partial = oni_bridge.read_new_records(log, old_position)
    require(len(truncated) == 1 and truncated[0]["session_id"] == "session-after-truncate", "truncation reset failed")

    malformed = root / "malformed" / "state.jsonl"
    malformed.parent.mkdir(parents=True)
    malformed.write_bytes(
        json_line(make_state("session-bad", 1))
        + b"\n{broken-json\n[]\n"
        + json_line(make_state("session-bad", 2, cycle=12))
        + b"\n"
    )
    records, _, invalid = oni_bridge.load_recent_records(malformed)
    runtime = oni_bridge.ReplayState()
    runtime.process_many(records)
    require(invalid == 2 and runtime.invalid_json_count == 2, "malformed line accounting is wrong")
    require(runtime.latest is not None and runtime.latest["seq"] == 2, "bad lines displaced the latest valid snapshot")
    return {
        "partial_bytes_observed": midpoint,
        "malformed_lines": invalid,
        "truncation_session": runtime.current_session_id,
    }


def case_sequence_and_session_isolation(root: pathlib.Path) -> Dict[str, Any]:
    runtime = oni_bridge.ReplayState()
    runtime.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "session-a", "seq": 1})
    runtime.process(make_state("session-a", 2, cycle=10, snapshot_seq=1))
    runtime.process(make_state("session-a", 2, cycle=99, snapshot_seq=1))
    runtime.process(make_state("session-a", 1, cycle=98, snapshot_seq=1))
    runtime.process(make_state("session-a", 5, cycle=11, snapshot_seq=2))
    require(runtime.latest is not None and runtime.latest["cycle"] == 11, "duplicate/out-of-order snapshot replaced current state")
    require(runtime.duplicate_seq_count == 1, "duplicate sequence was not counted")
    require(runtime.out_of_order_seq_count == 1, "out-of-order sequence was not counted")
    require(runtime.seq_gap_count == 1, "sequence gap was not counted")

    runtime.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "session-b", "seq": 1})
    require(runtime.latest is None, "new session reused an older session snapshot")
    runtime.process(make_state("session-a", 6, cycle=77))
    require(runtime.latest is None and runtime.current_session_id == "session-b", "retired session reclaimed current state")
    runtime.process(make_state("session-b", 2, cycle=12, snapshot_seq=1))
    require(runtime.latest is not None and runtime.latest["session_id"] == "session-b", "current session snapshot was not accepted")

    snapshot_gap = oni_bridge.ReplayState()
    snapshot_gap.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-snapshot-gap", "seq": 1}
    )
    snapshot_gap.process(make_state("session-snapshot-gap", 2, snapshot_seq=1))
    snapshot_gap.process(
        {"schema": "oni.event.v2", "event": "probe_patch_registered", "session_id": "session-snapshot-gap", "seq": 3}
    )
    snapshot_gap.process(make_state("session-snapshot-gap", 4, snapshot_seq=3))
    snapshot_gap_health = snapshot_gap.health(snapshot_gap.effective_latest(), {}, replay_mode=False)
    require(
        "snapshot_sequence_gap" in snapshot_gap_health["trust_blockers"],
        "snapshot-only sequence gap was hidden by a gap-free global event sequence",
    )
    require(snapshot_gap.snapshot_seq_gap_count == 1, "snapshot-only sequence gap was not counted")

    late_start = oni_bridge.ReplayState()
    late_start.process(make_state("session-late-start", 1, snapshot_seq=1))
    late_start.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-late-start", "seq": 2}
    )
    late_start_health = late_start.health(late_start.effective_latest(), {}, replay_mode=False)
    require(not late_start.session_start_observed, "late session_started retroactively certified a snapshot baseline")
    require(
        "session_start_order_invalid" in late_start_health["trust_blockers"],
        "late session_started did not leave a sticky trust blocker",
    )

    bad_first_start = oni_bridge.ReplayState()
    bad_first_start.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-bad-start", "seq": 2}
    )
    bad_first_health = bad_first_start.health(bad_first_start.effective_latest(), {}, replay_mode=False)
    require(bad_first_start.current_session_id is None, "session_started seq>1 established a session")
    require(not bad_first_health["trust_ready"], "session_started seq>1 produced trusted health")

    output = root / "session-output"
    waiting = oni_bridge.ReplayState()
    waiting.process(make_state("session-old", 1))
    waiting.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "session-new", "seq": 1})
    oni_bridge.write_outputs(output, waiting)
    health = json.loads((output / "health.json").read_text(encoding="utf-8"))
    summary = (output / "state_summary.md").read_text(encoding="utf-8")
    require(health["status"] == "waiting_current_session_snapshot", "waiting state has the wrong health status")
    require("will not reuse" in summary, "summary does not explain old-session isolation")
    require(json.loads((output / "latest_state.json").read_text(encoding="utf-8")) == {}, "old snapshot leaked into current output")
    return {
        "duplicate_seq_count": runtime.duplicate_seq_count,
        "out_of_order_seq_count": runtime.out_of_order_seq_count,
        "seq_gap_count": runtime.seq_gap_count,
        "snapshot_seq_gap_count": snapshot_gap.snapshot_seq_gap_count,
        "late_session_start_blocked": True,
        "first_session_start_seq_gt_one_blocked": True,
        "retired_session_event_count": runtime.retired_session_event_count,
        "waiting_status": health["status"],
    }


def case_stale_and_atomic_outputs(root: pathlib.Path) -> Dict[str, Any]:
    output = root / "stale-output"
    stale_time = iso_now(-120)
    runtime = oni_bridge.ReplayState()
    runtime.process(make_state("session-stale", 1, timestamp=stale_time))
    oni_bridge.write_outputs(output, runtime, replay_mode=False)

    health = json.loads((output / "health.json").read_text(encoding="utf-8"))
    latest = json.loads((output / "latest_state.json").read_text(encoding="utf-8"))
    require(health["status"] == "stale", "old snapshot was not marked stale")
    require(latest["quality"]["research.current"]["status"] == "stale", "field quality was not downgraded to stale")
    require(not list((output / "commands" / "pending").glob("*.json")), "stale state generated an action command")
    stale_packet = json.loads((output / "control-packet.json").read_text(encoding="utf-8-sig"))
    require(
        stale_packet.get("next_step") is None
        or stale_packet["next_step"].get("kind") != "action_candidate",
        "stale state produced a control-packet action candidate",
    )

    expected_json = (
        "latest_state.json",
        "advice.json",
        "incidents.json",
        "plan.json",
        "health.json",
        "strategy.json",
        "decision-frame.json",
        "control-packet.json",
    )
    session_output = output / "sessions" / "session-stale"
    for base in (output, session_output):
        for name in expected_json:
            json.loads((base / name).read_text(encoding="utf-8"))
        frame = json.loads((base / "decision-frame.json").read_text(encoding="utf-8"))
        validate_decision_frame(frame)
        require(frame["frame_id"] == health["decision_frame_id"], "health and canonical frame generation differ")
        require(not any(item["ready"] for item in frame["readiness"].values()), "stale canonical frame was actionable")
        summary = (base / "state_summary.md").read_text(encoding="utf-8")
        require(len(summary.encode("utf-8")) <= oni_bridge.MAX_SUMMARY_BYTES, "summary exceeded 8 KiB")
        require(len(summary.splitlines()) <= oni_bridge.MAX_SUMMARY_LINES, "summary exceeded 100 lines")
    require(not list(output.rglob("*.tmp")), "atomic output left temporary files behind")
    return {
        "health_status": health["status"],
        "snapshot_age_seconds": health["snapshot_age_seconds"],
        "pending_commands": 0,
    }


def case_health_publication_clock(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    observed_at = dt.datetime(2026, 8, 31, 9, 29, 36, 900_000, tzinfo=dt.timezone.utc)
    published_at = observed_at + dt.timedelta(microseconds=50_000)
    runtime = oni_bridge.ReplayState()
    runtime.process(
        make_state(
            "session-publication-clock",
            1,
            timestamp=observed_at.isoformat(timespec="microseconds"),
        )
    )
    latest = copy.deepcopy(runtime.latest)

    with mock.patch.object(oni_bridge, "local_now", return_value=published_at):
        health = runtime.health(latest, {}, replay_mode=False)

    projected_at = dt.datetime.fromisoformat(str(health["updated_at"]))
    require(
        health["updated_at"] == published_at.isoformat(timespec="microseconds"),
        "health publication timestamp lost sub-second precision",
    )
    require(
        projected_at >= observed_at,
        "same-second raw snapshot appeared after its health publication",
    )
    require(
        abs(float(health["snapshot_age_seconds"]) - 0.05) <= 0.001,
        "health age was not derived from the frozen publication clock",
    )
    require(
        projected_at.microsecond == 950_000,
        "health publication microseconds were truncated",
    )
    return {
        "observed_at": observed_at.isoformat(timespec="microseconds"),
        "published_at": health["updated_at"],
        "snapshot_age_seconds": health["snapshot_age_seconds"],
    }


def case_external_startup_binding_gate(root: pathlib.Path) -> Dict[str, Any]:
    expected = startup_expectations()

    missing = oni_bridge.ReplayState(require_startup_binding=True)
    missing.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "missing", "seq": 1})
    missing.process(make_state("missing", 2, snapshot_seq=1))
    missing_health = missing.health(missing.effective_latest(), {}, replay_mode=False)
    require(
        "startup_expectations_missing" in missing_health["trust_blockers"],
        "live Bridge accepted a session without external startup expectations",
    )
    missing_output = root / "startup-binding-missing-output"
    oni_bridge.write_outputs(missing_output, missing)
    published_health = json.loads((missing_output / "health.json").read_text(encoding="utf-8"))
    published_latest = json.loads((missing_output / "latest_state.json").read_text(encoding="utf-8"))
    published_frame = json.loads((missing_output / "decision-frame.json").read_text(encoding="utf-8"))
    require(
        "startup_expectations_missing" in published_health["trust_blockers"]
        and "startup_expectations_missing" in published_latest["bridge"]["trust_blockers"],
        "startup blocker did not propagate to health and latest state",
    )
    require(
        not any(item["ready"] for item in published_frame["readiness"].values()),
        "startup-unbound decision frame remained actionable",
    )
    require(
        not list((missing_output / "commands" / "pending").glob("*.json")),
        "startup-unbound output queued a pending command",
    )

    valid = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    start = attach_startup_contract(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "reason": "save_loader_load_complete",
            "session_id": "startup-a",
            "seq": 1,
            "game_build": expected["game_build"],
            "mod_version": expected["mod_version"],
            "save_id_hash": expected["loader_save_sha256"],
        },
        expected,
    )
    pending_process = attach_startup_contract(
        {
            "schema": "oni.event.v2",
            "event": "probe_patch_registered",
            "session_id": "process-before-target",
            "seq": 1,
            "game_build": expected["game_build"],
            "mod_version": expected["mod_version"],
            "save_id_hash": None,
        },
        expected,
    )
    pending_process["startup_binding_status"] = "pending_request"
    pending_process["requested_save_path_id"] = None
    pending_process["active_save_path_id"] = None
    valid.process(pending_process)
    require(valid.current_session_id is None, "pre-session process envelope established a session")
    require(not valid.startup_integrity_blockers, "pending_request/null paths poisoned startup binding")
    valid.process(start)
    valid.process(
        startup_snapshot(
            expected,
            "startup-a",
            2,
            1,
            active_path_id=expected["loader_path_id_upper"],
            save_id_hash=expected["loader_save_sha256"],
        )
    )
    valid_health = valid.health(valid.effective_latest(), {}, replay_mode=False)
    require(valid_health["status"] == "ok", "exact external startup contract was not trusted")
    require(valid_health["startup_binding"]["verified"], "verified startup summary was not published")
    require(valid_health["startup_binding"]["validation_process_id"] == 4242, "startup PID was not frozen")

    # A same-path reload can advance only after the synchronous save checkpoint
    # triple proves the new bytes, followed by the Probe's unload/end boundary.
    same_path_save = "sha256:" + "6" * 64
    valid.process_many(
        startup_save_checkpoint(
            expected,
            "startup-a",
            3,
            control_id="same-path-save-a",
            active_before=expected["loader_path_id_upper"],
            target_path_id=expected["loader_path_id_upper"],
            save_before=expected["loader_save_sha256"],
            save_after=same_path_save,
        )
    )
    valid.process_many(
        startup_reload_boundary(
            expected,
            "startup-a",
            6,
            "startup-b",
            active_path_id=expected["loader_path_id_upper"],
            save_id_hash=same_path_save,
        )
    )
    reload_health = valid.health(valid.effective_latest(), {}, replay_mode=False)
    require(reload_health["status"] == "ok", "same-path post-save reload invalidated startup proof")
    require(
        reload_health["startup_binding"]["verified_session_id"] == "startup-b",
        "verified session id did not advance after an authorized in-process reload",
    )
    post_target_process = copy.deepcopy(pending_process)
    post_target_process["session_id"] = "process-after-target"
    post_target_process["seq"] = 1
    valid.process(post_target_process)
    post_process_health = valid.health(valid.effective_latest(), {}, replay_mode=False)
    require(post_process_health["status"] == "ok", "post-target process envelope poisoned trust")
    require(
        post_process_health["startup_binding"]["verified_session_id"] == "startup-b",
        "post-target process envelope displaced the verified target session",
    )

    wrong_reason = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    wrong_reason_start = copy.deepcopy(start)
    wrong_reason_start["session_id"] = "wrong-reason"
    wrong_reason_start["reason"] = "game_lifecycle_patch"
    wrong_reason.process(wrong_reason_start)
    wrong_reason_health = wrong_reason.health(wrong_reason.effective_latest(), {}, replay_mode=False)
    require(wrong_reason.current_session_id is None, "wrong-reason start established a live session")
    require(
        "startup_start_reason_invalid" in wrong_reason_health["trust_blockers"],
        "wrong-reason target start was not fail-closed",
    )

    external_pid = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    wrong_external_pid_start = attach_startup_contract(
        copy.deepcopy(start), expected, process_id=4343
    )
    wrong_external_pid_start["session_id"] = "wrong-external-pid"
    external_pid.process(wrong_external_pid_start)
    external_pid_health = external_pid.health(
        external_pid.effective_latest(), {}, replay_mode=False
    )
    require(
        external_pid.current_session_id is None
        and "startup_process_id_mismatch" in external_pid_health["trust_blockers"],
        "first target session did not bind to the externally supplied PID",
    )
    recovered_external_pid_start = copy.deepcopy(start)
    recovered_external_pid_start["session_id"] = "correct-after-wrong-external-pid"
    external_pid.process(recovered_external_pid_start)
    external_pid_health_after_retry = external_pid.health(
        external_pid.effective_latest(), {}, replay_mode=False
    )
    require(
        external_pid.current_session_id is None
        and "startup_process_id_mismatch"
        in external_pid_health_after_retry["trust_blockers"]
        and not external_pid_health_after_retry["trust_ready"],
        "external PID mismatch recovered after a later correct target start",
    )

    variants = {
        "validation_run_id": "startup_validation_run_mismatch",
        "startup_binding_hash": "startup_binding_hash_mismatch",
        "startup_intent_hash": "startup_intent_hash_mismatch",
        "runtime_config_evidence_sha256": "startup_runtime_config_hash_mismatch",
        "requested_save_path_id": "startup_requested_path_mismatch",
        "active_save_path_id": "startup_active_path_mismatch",
        "game_build": "startup_game_build_mismatch",
        "mod_version": "startup_mod_version_mismatch",
        "startup_binding_status": "startup_binding_unverified",
    }
    observed: Dict[str, str] = {}
    for field, blocker in variants.items():
        runtime = oni_bridge.ReplayState(
            require_startup_binding=True,
            startup_expectations=expected,
        )
        broken = copy.deepcopy(start)
        broken["session_id"] = "broken-" + field.replace("_", "-")
        if field.endswith("hash") or field.endswith("sha256") or field.endswith("path_id"):
            broken[field] = "sha256:" + "f" * 64
        else:
            broken[field] = "wrong"
        runtime.process(broken)
        fixed_snapshot = attach_startup_contract(
            make_state(str(broken["session_id"]), 2, snapshot_seq=1),
            expected,
        )
        runtime.process(fixed_snapshot)
        health = runtime.health(runtime.effective_latest(), {}, replay_mode=False)
        require(blocker in health["trust_blockers"], f"{field} mismatch was not sticky")
        require(not health["trust_ready"], f"{field} mismatch recovered after a later correct record")
        observed[field] = blocker

    pid_runtime = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    pid_runtime.process(start)
    pid_runtime.process(
        attach_startup_contract(make_state("startup-a", 2, snapshot_seq=1), expected, process_id=4343)
    )
    pid_health = pid_runtime.health(pid_runtime.effective_latest(), {}, replay_mode=False)
    require("startup_process_id_mismatch" in pid_health["trust_blockers"], "PID drift was not sticky")

    path_runtime = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    path_runtime.process(start)
    path_drift = attach_startup_contract(make_state("startup-a", 2, snapshot_seq=1), expected)
    path_drift["active_save_path_id"] = "sha256:" + "d" * 64
    path_runtime.process(path_drift)
    path_runtime.process(attach_startup_contract(make_state("startup-a", 3, snapshot_seq=2), expected))
    path_health = path_runtime.health(path_runtime.effective_latest(), {}, replay_mode=False)
    require(
        "startup_active_path_mismatch" in path_health["trust_blockers"],
        "post-start active path drift was not sticky",
    )

    save_runtime = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    wrong_save_start = copy.deepcopy(start)
    wrong_save_start["save_id_hash"] = "sha256:" + "e" * 64
    save_runtime.process(wrong_save_start)
    save_health = save_runtime.health(save_runtime.effective_latest(), {}, replay_mode=False)
    require(
        "startup_initial_save_hash_mismatch" in save_health["trust_blockers"],
        "initial loader content mismatch was not blocked",
    )

    try:
        oni_bridge.validate_startup_expectations({"validation_run_id": expected["validation_run_id"]})
    except ValueError:
        partial_rejected = True
    else:
        partial_rejected = False
    require(partial_rejected, "partial external expectation set was accepted")

    invalid_pid_expectations_rejected = 0
    for invalid_pid in (True, "4242", 0, -1):
        invalid_expected = dict(expected)
        invalid_expected["validation_process_id"] = invalid_pid
        try:
            oni_bridge.validate_startup_expectations(invalid_expected)
        except ValueError:
            invalid_pid_expectations_rejected += 1
    require(
        invalid_pid_expectations_rejected == 4,
        "external PID expectation accepted bool/string/non-positive input",
    )

    return {
        "exact_contract_status": valid_health["status"],
        "same_path_reload_status": reload_health["status"],
        "sticky_mismatch_count": len(observed) + 5,
        "missing_external_expectations_blocked": True,
        "pending_request_null_paths_ignored": True,
        "wrong_reason_start_blocked": True,
        "reload_verified_session_advanced": True,
        "post_target_process_envelope_ignored": True,
        "health_state_frame_propagation": True,
        "partial_expectations_rejected": partial_rejected,
        "external_pid_rejections": invalid_pid_expectations_rejected,
    }


def case_authorized_derivative_transition(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    expected = startup_expectations()
    records, anchors = authorized_derivative_chain(expected)

    runtime = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    runtime.process_many(records)
    health = runtime.health(runtime.effective_latest(), {}, replay_mode=False)
    startup_health = health["startup_binding"]
    require(health["status"] == "ok" and health["trust_ready"], "four-session chain was not trusted")
    require(not runtime.startup_integrity_blockers, "valid derivative chain produced a startup blocker")
    require(runtime.current_session_id == anchors["final_session"], "final reload session was not selected")
    require(
        runtime.startup_requested_save_path_id == anchors["loader_path"],
        "process-scoped requested loader anchor moved",
    )
    require(
        runtime.startup_current_active_save_path_id == anchors["derivative_path"],
        "authorized derivative path did not become the current active path",
    )
    require(
        runtime.startup_current_save_id_hash == anchors["inverse_save"],
        "same-path forward/inverse checkpoints did not advance the current save tip",
    )
    require(
        runtime.startup_distinct_path_transition_count == 1,
        "valid chain did not contain exactly one loader-to-derivative transition",
    )
    require(
        startup_health["verified_session_id"] == anchors["final_session"]
        and startup_health["requested_save_path_id"] == anchors["loader_path"]
        and startup_health["current_active_save_path_id"] == anchors["derivative_path"]
        and startup_health["current_save_id_hash"] == anchors["inverse_save"]
        and startup_health["distinct_path_transition_count"] == 1
        and startup_health["save_evidence_pending"] is False
        and startup_health["reload_transition_ready"] is False,
        "health did not publish the reconstructed process-scoped transition tip",
    )

    application_quit = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    application_quit.process_many(copy.deepcopy(records))
    application_quit.process(
        startup_event(
            expected,
            anchors["final_session"],
            4,
            "session_ended",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
            reason="application_quit",
        )
    )
    application_quit.process(
        startup_event(
            expected,
            anchors["final_session"],
            5,
            "probe_shutdown",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
        )
    )
    application_quit_health = application_quit.health(
        application_quit.effective_latest(), {}, replay_mode=False
    )
    require(
        not application_quit.startup_integrity_blockers
        and application_quit_health["status"] == "session_ended",
        "normal application_quit tail invalidated the startup chain",
    )

    lifecycle_stop = oni_bridge.ReplayState(
        require_startup_binding=True,
        startup_expectations=expected,
    )
    lifecycle_stop.process_many(copy.deepcopy(records))
    lifecycle_stop.process(
        startup_event(
            expected,
            anchors["final_session"],
            4,
            "save_unloaded",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
            reason="game_lifecycle_patch",
        )
    )
    lifecycle_stop.process(
        startup_event(
            expected,
            anchors["final_session"],
            5,
            "session_ended",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
            reason="game_lifecycle_patch",
        )
    )
    lifecycle_stop_health = lifecycle_stop.health(
        lifecycle_stop.effective_latest(), {}, replay_mode=False
    )
    require(
        not lifecycle_stop.startup_integrity_blockers
        and lifecycle_stop_health["status"] == "session_ended",
        "normal lifecycle stop tail was mistaken for an unauthorized reload",
    )

    source = root / "state.jsonl"
    source.write_bytes(b"".join(json_line(record) + b"\n" for record in records))
    output = root / "output"
    replayed, position = oni_bridge.replay_initial_records(
        source,
        output,
        startup_expectations=expected,
        require_startup_binding=True,
    )
    require(position == source.stat().st_size, "initial replay cursor did not reach the complete input")
    replay_health = replayed.health(replayed.effective_latest(), {}, replay_mode=False)
    require(replay_health["status"] == "ok", "complete restart replay did not rebuild trusted state")
    require(
        replayed.startup_requested_save_path_id == runtime.startup_requested_save_path_id
        and replayed.startup_current_active_save_path_id
        == runtime.startup_current_active_save_path_id
        and replayed.startup_current_save_id_hash == runtime.startup_current_save_id_hash
        and replayed.startup_distinct_path_transition_count
        == runtime.startup_distinct_path_transition_count,
        "Bridge restart reconstructed different process-scoped anchors",
    )

    archived_before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in sorted((output / "sessions").rglob("state.jsonl"))
    }
    source_archive = output / "sessions" / anchors["origin_session"] / "state.jsonl"
    source_archive_metadata = source_archive.stat()
    source_archive_identity = (
        int(source_archive_metadata.st_dev),
        int(source_archive_metadata.st_ino),
    )
    source_archive_version = (
        int(source_archive_metadata.st_dev),
        int(source_archive_metadata.st_ino),
        int(source_archive_metadata.st_nlink),
        int(source_archive_metadata.st_size),
        int(source_archive_metadata.st_mtime_ns),
        int(source_archive_metadata.st_ctime_ns),
    )
    source_archive_bytes = source_archive.read_bytes()
    source_archive_sha256 = hashlib.sha256(source_archive_bytes).hexdigest()
    source_archive_record_count = sum(
        1 for record in records if record.get("session_id") == anchors["origin_session"]
    )
    require(len(expected) == 9, "restart fixture did not reuse the exact nine startup anchors")
    require(
        len(source_archive_bytes.splitlines()) == source_archive_record_count,
        "S0 source_archive did not contain every replayed S0 record",
    )
    require(not list(output.rglob("*.tmp")), "Bridge stop boundary retained an archive temporary")

    restart_replace_calls = 0
    original_replace = pathlib.Path.replace

    def observe_restart_replace(
        temporary: pathlib.Path,
        destination: pathlib.Path,
    ) -> pathlib.Path:
        nonlocal restart_replace_calls
        restart_replace_calls += 1
        return original_replace(temporary, destination)

    with mock.patch.object(pathlib.Path, "replace", new=observe_restart_replace):
        replayed_again, position_again = oni_bridge.replay_initial_records(
            source,
            output,
            startup_expectations=copy.deepcopy(expected),
            require_startup_binding=True,
        )
    archived_after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in sorted((output / "sessions").rglob("state.jsonl"))
    }
    source_archive_after_metadata = source_archive.stat()
    require(position_again == position, "repeat replay produced a different complete cursor")
    require(archived_after == archived_before, "restart replay duplicated or rewrote session archives")
    require(restart_replace_calls == 0, "complete canonical restart replay replaced a session archive")
    require(
        (
            int(source_archive_after_metadata.st_dev),
            int(source_archive_after_metadata.st_ino),
        )
        == source_archive_identity,
        "restart replay changed the S0 source_archive file identity",
    )
    require(
        (
            int(source_archive_after_metadata.st_dev),
            int(source_archive_after_metadata.st_ino),
            int(source_archive_after_metadata.st_nlink),
            int(source_archive_after_metadata.st_size),
            int(source_archive_after_metadata.st_mtime_ns),
            int(source_archive_after_metadata.st_ctime_ns),
        )
        == source_archive_version,
        "restart replay changed the S0 source_archive identity/version fields",
    )
    require(source_archive.read_bytes() == source_archive_bytes, "restart replay changed S0 archive bytes")
    require(
        hashlib.sha256(source_archive.read_bytes()).hexdigest() == source_archive_sha256,
        "restart replay changed the S0 source_archive raw SHA-256",
    )
    require(
        replayed_again.startup_current_active_save_path_id == anchors["derivative_path"]
        and replayed_again.startup_current_save_id_hash == anchors["inverse_save"],
        "repeat replay failed to rebuild the same derivative tip",
    )

    oni_bridge.write_outputs(output, replayed_again, replay_mode=False)
    published_health = json.loads((output / "health.json").read_text(encoding="utf-8"))
    decision_frame = json.loads((output / "decision-frame.json").read_text(encoding="utf-8"))
    validate_decision_frame(decision_frame)
    require(
        published_health["status"] == "ok" and published_health["trust_ready"],
        "restart output health was not trust-ready",
    )
    require(
        decision_frame["trust"]["status"] == "trusted"
        and decision_frame["trust"]["health_ok"] is True,
        "restart decision frame did not preserve trusted source identity",
    )
    replayed_again.process(
        startup_event(
            expected,
            anchors["final_session"],
            4,
            "session_ended",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
            reason="application_quit",
        )
    )
    replayed_again.process(
        startup_event(
            expected,
            anchors["final_session"],
            5,
            "probe_shutdown",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["inverse_save"],
        )
    )
    restart_quit_health = replayed_again.health(
        replayed_again.effective_latest(), {}, replay_mode=False
    )
    require(
        not replayed_again.startup_integrity_blockers
        and restart_quit_health["status"] == "session_ended",
        "Bridge restart followed by normal quit invalidated the replayed chain",
    )
    require(not list(output.rglob("*.tmp")), "restart replay/output left temporary files")

    def find_index(
        values: List[Dict[str, Any]],
        *,
        event: str,
        session_id: str | None = None,
        control_id: str | None = None,
    ) -> int:
        for index, value in enumerate(values):
            if value.get("event") != event:
                continue
            if session_id is not None and value.get("session_id") != session_id:
                continue
            if control_id is not None and value.get("control_id") != control_id:
                continue
            return index
        raise AssertionError(f"fixture record not found: {event}/{session_id}/{control_id}")

    def blocked(
        name: str,
        values: List[Dict[str, Any]],
        expected_blocker: str,
    ) -> str:
        negative = oni_bridge.ReplayState(
            require_startup_binding=True,
            startup_expectations=expected,
        )
        negative.process_many(values)
        negative_health = negative.health(negative.effective_latest(), {}, replay_mode=False)
        require(
            expected_blocker in negative_health["trust_blockers"],
            f"{name} did not produce {expected_blocker}: {negative_health['trust_blockers']}",
        )
        require(not negative_health["trust_ready"], f"{name} recovered after later valid records")
        return expected_blocker

    negative_results: Dict[str, str] = {}

    no_control = copy.deepcopy(records)
    del no_control[
        find_index(no_control, event="validation_control_started", control_id="origin-save-as")
    ]
    negative_results["missing_control"] = blocked(
        "missing_control", no_control, "startup_save_start_invalid"
    )

    wrong_target = copy.deepcopy(records)
    wrong_target[
        find_index(wrong_target, event="validation_control_started", control_id="origin-save-as")
    ]["target_save_path_id"] = "sha256:" + "e" * 64
    negative_results["wrong_target"] = blocked(
        "wrong_target", wrong_target, "startup_save_start_invalid"
    )

    out_of_order = copy.deepcopy(records)
    control_index = find_index(
        out_of_order, event="validation_control_started", control_id="origin-save-as"
    )
    out_of_order[control_index], out_of_order[control_index + 1] = (
        out_of_order[control_index + 1],
        out_of_order[control_index],
    )
    negative_results["out_of_order"] = blocked(
        "out_of_order", out_of_order, "startup_save_start_invalid"
    )

    duplicate_control = copy.deepcopy(records)
    control_index = find_index(
        duplicate_control, event="validation_control_started", control_id="origin-save-as"
    )
    duplicate_control.insert(control_index + 1, copy.deepcopy(duplicate_control[control_index]))
    negative_results["duplicate_control"] = blocked(
        "duplicate_control", duplicate_control, "startup_save_evidence_order_invalid"
    )

    failed_completion = copy.deepcopy(records)
    failed_completion[
        find_index(failed_completion, event="save_write_completed", session_id=anchors["origin_session"])
    ]["success"] = False
    negative_results["failed_completion"] = blocked(
        "failed_completion", failed_completion, "startup_save_completion_invalid"
    )

    nonverified_completion = copy.deepcopy(records)
    nonverified_completion[
        find_index(
            nonverified_completion,
            event="save_write_completed",
            session_id=anchors["origin_session"],
        )
    ]["startup_binding_status"] = "failed"
    negative_results["nonverified_completion"] = blocked(
        "nonverified_completion", nonverified_completion, "startup_binding_unverified"
    )

    hash_mismatch = copy.deepcopy(records)
    hash_mismatch[
        find_index(hash_mismatch, event="save_write_completed", session_id=anchors["origin_session"])
    ]["active_save_id_hash"] = "sha256:" + "f" * 64
    negative_results["hash_mismatch"] = blocked(
        "hash_mismatch", hash_mismatch, "startup_save_completion_invalid"
    )

    completion_refusals: Dict[str, tuple[str, Any]] = {
        "auto_save_completion": ("is_auto_save", True),
        "pointer_not_updated": ("update_save_pointer", False),
        "unreadable_save": ("save_file_readable", False),
        "rewrite_not_observed": ("file_rewrite_observed", False),
        "result_path_mismatch": ("result_save_path_id", "sha256:" + "f" * 64),
        "before_hash_mismatch": ("before_save_id_hash", "sha256:" + "f" * 64),
        "written_hash_invalid": ("written_save_id_hash", "sha256:not-a-hash"),
        "envelope_hash_mismatch": ("save_id_hash", "sha256:" + "f" * 64),
    }
    for name, (field, value) in completion_refusals.items():
        broken_completion = copy.deepcopy(records)
        broken_completion[
            find_index(
                broken_completion,
                event="save_write_completed",
                session_id=anchors["origin_session"],
            )
        ][field] = value
        negative_results[name] = blocked(
            name,
            broken_completion,
            "startup_save_completion_invalid",
        )

    active_jump = copy.deepcopy(records)
    activation_snapshot = find_index(
        active_jump, event="state_snapshot", session_id=anchors["activation_session"]
    )
    active_jump[activation_snapshot]["active_save_path_id"] = "sha256:" + "f" * 64
    negative_results["active_jump"] = blocked(
        "active_jump", active_jump, "startup_active_path_mismatch"
    )

    requested_jump = copy.deepcopy(records)
    activation_snapshot = find_index(
        requested_jump, event="state_snapshot", session_id=anchors["activation_session"]
    )
    requested_jump[activation_snapshot]["requested_save_path_id"] = anchors["derivative_path"]
    negative_results["requested_anchor_jump"] = blocked(
        "requested_anchor_jump", requested_jump, "startup_requested_path_mismatch"
    )

    reload_start_active_mismatch = copy.deepcopy(records)
    reload_start_index = find_index(
        reload_start_active_mismatch,
        event="session_started",
        session_id=anchors["activation_session"],
    )
    reload_start_active_mismatch[reload_start_index]["active_save_path_id"] = (
        "sha256:" + "f" * 64
    )
    negative_results["reload_start_active_mismatch"] = blocked(
        "reload_start_active_mismatch",
        reload_start_active_mismatch,
        "startup_reload_active_path_mismatch",
    )

    reload_start_hash_mismatch = copy.deepcopy(records)
    reload_start_index = find_index(
        reload_start_hash_mismatch,
        event="session_started",
        session_id=anchors["activation_session"],
    )
    reload_start_hash_mismatch[reload_start_index]["save_id_hash"] = "sha256:" + "f" * 64
    negative_results["reload_start_hash_mismatch"] = blocked(
        "reload_start_hash_mismatch",
        reload_start_hash_mismatch,
        "startup_reload_save_hash_mismatch",
    )

    session_jump = copy.deepcopy(records)
    session_jump = [
        value
        for value in session_jump
        if not (
            value.get("session_id") == anchors["origin_session"]
            and value.get("event") in {"save_unloaded", "session_ended"}
        )
    ]
    negative_results["session_jump"] = blocked(
        "session_jump", session_jump, "startup_reload_transition_missing"
    )

    post_end_record = copy.deepcopy(records)
    origin_end = find_index(
        post_end_record, event="session_ended", session_id=anchors["origin_session"]
    )
    post_end_record.insert(
        origin_end + 1,
        startup_event(
            expected,
            anchors["origin_session"],
            11,
            "probe_shutdown",
            active_path_id=anchors["derivative_path"],
            save_id_hash=anchors["derivative_save"],
        ),
    )
    negative_results["record_after_reload_end"] = blocked(
        "record_after_reload_end",
        post_end_record,
        "startup_reload_evidence_order_invalid",
    )

    duplicate_transition = copy.deepcopy(records)
    duplicate_transition[
        find_index(
            duplicate_transition,
            event="validation_control_started",
            control_id="forward-save",
        )
    ]["target_save_path_id"] = "sha256:" + "e" * 64
    negative_results["duplicate_transition"] = blocked(
        "duplicate_transition",
        duplicate_transition,
        "startup_duplicate_path_transition",
    )

    truncated_records = [
        copy.deepcopy(value)
        for value in records
        if value.get("session_id") == anchors["final_session"]
    ]
    truncated_source = root / "truncated-state.jsonl"
    truncated_source.write_bytes(
        b"".join(json_line(record) + b"\n" for record in truncated_records)
    )
    truncated, _ = oni_bridge.replay_initial_records(
        truncated_source,
        root / "truncated-output",
        startup_expectations=expected,
        require_startup_binding=True,
    )
    truncated_health = truncated.health(truncated.effective_latest(), {}, replay_mode=False)
    require(
        "startup_active_path_mismatch" in truncated_health["trust_blockers"]
        and "startup_initial_save_hash_mismatch" in truncated_health["trust_blockers"],
        "truncated restart replay accepted an unproven derivative session",
    )
    require(not truncated_health["trust_ready"], "truncated restart replay became trust-ready")
    negative_results["restart_truncated"] = "startup_initial_save_hash_mismatch"

    return {
        "session_count": 4,
        "record_count": len(records),
        "authorized_distinct_path_transitions": 1,
        "same_path_checkpoint_count": 2,
        "restart_replay_stable": True,
        "restart_archive_replace_calls": restart_replace_calls,
        "source_archive_identity_preserved": True,
        "source_archive_identity_version_preserved": True,
        "source_archive_record_count": source_archive_record_count,
        "source_archive_sha256": source_archive_sha256,
        "restart_then_normal_exit": True,
        "decision_trust_status": decision_frame["trust"]["status"],
        "normal_exit_tails_accepted": 2,
        "negative_count": len(negative_results),
        "negative_results": negative_results,
    }


def case_strict_snapshot_admission(root: pathlib.Path) -> Dict[str, Any]:
    base = make_state("session-trust", 2, snapshot_seq=1)
    require(
        state_quality.snapshot_trust_blockers(base) == [],
        "zeroed writer diagnostics did not produce a clean admission baseline",
    )
    variants: Dict[str, tuple[Dict[str, Any], str]] = {}

    def add(name: str, expected: str, mutate: Callable[[Dict[str, Any]], None]) -> None:
        snapshot = copy.deepcopy(base)
        mutate(snapshot)
        variants[name] = (snapshot, expected)

    add("timestamp_missing", "timestamp_invalid", lambda value: value.pop("ts"))
    add("timestamp_bad_text", "timestamp_invalid", lambda value: value.__setitem__("ts", "not-a-time"))
    add(
        "timestamp_without_timezone",
        "timestamp_invalid",
        lambda value: value.__setitem__("ts", dt.datetime.now().replace(microsecond=0).isoformat()),
    )
    add("timestamp_in_future", "timestamp_in_future", lambda value: value.__setitem__("ts", iso_now(3600)))
    add("sample_missing", "sample_interval_invalid", lambda value: value.pop("sample"))
    add("cycle_string", "cycle_invalid", lambda value: value.__setitem__("cycle", "bogus"))
    add("cycle_fractional", "cycle_invalid", lambda value: value.__setitem__("cycle", 10.5))
    add("cycle_non_finite", "cycle_invalid", lambda value: value.__setitem__("cycle", float("nan")))
    add("cycle_boolean", "cycle_invalid", lambda value: value.__setitem__("cycle", True))
    add("cycle_huge_integer", "cycle_invalid", lambda value: value.__setitem__("cycle", 10**10000))
    add("game_time_cycles_object", "game_time_cycles_invalid", lambda value: value.__setitem__("game_time_cycles", {}))
    add("game_time_cycles_non_finite", "game_time_cycles_invalid", lambda value: value.__setitem__("game_time_cycles", float("inf")))
    add("game_time_cycles_boolean", "game_time_cycles_invalid", lambda value: value.__setitem__("game_time_cycles", False))
    add("game_time_cycles_negative", "game_time_cycles_invalid", lambda value: value.__setitem__("game_time_cycles", -1))
    fractional_clock = copy.deepcopy(base)
    fractional_clock["game_time_cycles"] = 10.5
    require("game_time_cycles_invalid" not in state_quality.snapshot_trust_blockers(fractional_clock),
            "native fractional game time was incorrectly rejected")
    for name, interval in (
        ("sample_string", "5"),
        ("sample_bool", True),
        ("sample_zero", 0),
        ("sample_too_small", 0.09),
        ("sample_too_large", 60.01),
        ("sample_huge_integer", 10**10000),
        ("sample_nan", float("nan")),
        ("sample_infinite", float("inf")),
    ):
        add(name, "sample_interval_invalid", lambda value, item=interval: value["sample"].__setitem__("interval_seconds", item))
    add("sequence_missing", "sequence_invalid", lambda value: value.pop("seq"))
    for name, sequence in (
        ("sequence_string", "2"),
        ("sequence_bool", True),
        ("sequence_zero", 0),
        ("sequence_float", 2.0),
    ):
        add(name, "sequence_invalid", lambda value, item=sequence: value.__setitem__("seq", item))
    add("snapshot_sequence_missing", "snapshot_sequence_invalid", lambda value: value.pop("snapshot_seq"))
    for name, snapshot_sequence in (
        ("snapshot_sequence_string", "2"),
        ("snapshot_sequence_bool", True),
        ("snapshot_sequence_zero", 0),
        ("snapshot_sequence_float", 2.0),
        ("snapshot_sequence_after_event_sequence", 3),
    ):
        add(
            name,
            "snapshot_sequence_invalid",
            lambda value, item=snapshot_sequence: value.__setitem__("snapshot_seq", item),
        )
    add("snapshot_id_wrong", "snapshot_id_invalid", lambda value: value.__setitem__("snapshot_id", "session-trust:999"))
    add("envelope_schema", "snapshot_envelope_invalid", lambda value: value.__setitem__("schema", "oni.event.v2"))
    add("envelope_event", "snapshot_envelope_invalid", lambda value: value.__setitem__("event", "command_ack"))
    add(
        "active_world_identity_mismatch",
        "active_world_identity_mismatch",
        lambda value: value.__setitem__("observation_scope", {"active_world_id": 1}),
    )
    add(
        "forged_multiworld_complete_scope",
        "covered_world_ids_not_active_world_only",
        lambda value: value.__setitem__(
            "observation_scope",
            {
                "active_world_id": 0,
                "populated_world_ids": [1],
                "covered_world_ids": [0, 1],
                "complete_for_populated_worlds": True,
            },
        ),
    )
    add("writer_missing", "writer_status_missing", lambda value: value.pop("writer"))
    for name, dropped in (
        ("writer_drop_positive", 1),
        ("writer_drop_negative", -1),
        ("writer_drop_string", "0"),
        ("writer_drop_float", 0.5),
        ("writer_drop_bool", False),
    ):
        expected = "writer_dropped_snapshots" if dropped == 1 else "writer_drop_count_invalid"
        add(name, expected, lambda value, item=dropped: value["writer"].__setitem__("dropped_snapshot_count", item))
    for name, queue_depth in (
        ("writer_queue_missing", None),
        ("writer_queue_negative", -1),
        ("writer_queue_string", "0"),
        ("writer_queue_bool", False),
        ("writer_queue_positive", 1),
    ):
        expected = "writer_queue_not_empty" if queue_depth == 1 else "writer_queue_depth_invalid"
        if queue_depth is None:
            add(name, "writer_queue_depth_invalid", lambda value: value["writer"].pop("queue_depth"))
        else:
            add(name, expected, lambda value, item=queue_depth: value["writer"].__setitem__("queue_depth", item))
    for field, prefix, invalid_blocker, positive_blocker in (
        ("fault_count", "writer_fault_count", "writer_fault_count_invalid", "writer_faults_detected"),
        (
            "non_finite_numeric_count",
            "writer_non_finite_count",
            "writer_non_finite_count_invalid",
            "writer_non_finite_numbers",
        ),
    ):
        for suffix, count in (
            ("missing", None),
            ("negative", -1),
            ("string", "0"),
            ("bool", False),
            ("positive", 1),
        ):
            name = f"{prefix}_{suffix}"
            expected = positive_blocker if count == 1 else invalid_blocker
            if count is None:
                add(name, expected, lambda value, key=field: value["writer"].pop(key))
            else:
                add(
                    name,
                    expected,
                    lambda value, key=field, item=count: value["writer"].__setitem__(key, item),
                )
    for name, fault in (
        ("writer_fault", "append_failed"),
        ("writer_fault_integer_zero", 0),
        ("writer_fault_float_zero", 0.0),
    ):
        add(name, "writer_fault", lambda value, item=fault: value["writer"].__setitem__("fault", item))

    require(
        oni_bridge.snapshot_trust_blockers is state_quality.snapshot_trust_blockers,
        "Bridge does not use the shared state-quality admission implementation",
    )
    nonfinite_record, nonfinite_bad = oni_bridge.parse_json_line(
        '{"schema":"oni.state.v2","event":"state_snapshot","value":NaN}'
    )
    require(
        nonfinite_bad and nonfinite_record is not None and nonfinite_record.get("event") == "invalid_json",
        "non-standard non-finite JSON constant was accepted",
    )
    observed: Dict[str, List[str]] = {}
    for name, (snapshot, expected) in variants.items():
        blockers = state_quality.snapshot_trust_blockers(snapshot)
        observed[name] = blockers
        require(expected in blockers, f"{name} was not rejected with {expected}")

    require(
        state_quality.snapshot_age_seconds(variants["timestamp_in_future"][0]) is None,
        "future timestamp retained a usable age",
    )
    require(
        state_quality.snapshot_age_seconds(variants["timestamp_without_timezone"][0]) is None,
        "timezone-free timestamp retained a usable age",
    )
    require(
        state_quality.stale_after_seconds(variants["sample_too_large"][0]) == 0.0,
        "invalid sample interval retained a permissive stale threshold",
    )

    live_cases = (
        "timestamp_bad_text",
        "sample_too_large",
        "envelope_schema",
        "sequence_string",
        "snapshot_sequence_string",
        "active_world_identity_mismatch",
        "forged_multiworld_complete_scope",
        "writer_queue_positive",
        "writer_fault_count_positive",
        "writer_non_finite_count_positive",
        "writer_fault",
    )
    live_statuses: Dict[str, str] = {}
    for name in live_cases:
        runtime = oni_bridge.ReplayState()
        runtime.process(
            {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-trust", "seq": 1}
        )
        snapshot, expected = variants[name]
        if name == "writer_fault":
            snapshot["resources"]["food_kcal"] = 1.0
        runtime.process(snapshot)
        latest = runtime.effective_latest()
        health = runtime.health(latest, {}, replay_mode=False)
        live_statuses[name] = str(health["status"])
        require(health["trust_ready"] is False, f"{name} became trust-ready")
        require(expected in health["trust_blockers"], f"{name} blocker was lost by ReplayState")
        output = root / f"live-{name}"
        oni_bridge.write_outputs(output, runtime, replay_mode=False)
        require(not list((output / "commands" / "pending").glob("*.json")), f"{name} generated a command")
        advice = json.loads((output / "advice.json").read_text(encoding="utf-8"))
        require(not advice.get("items"), f"{name} generated executable advice")

    hijack = oni_bridge.ReplayState()
    hijack.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "session-a", "seq": 1})
    hijack.process({"schema": "wrong", "event": "session_started", "session_id": "session-b", "seq": 1})
    require(hijack.current_session_id == "session-a", "malformed session-start envelope hijacked the live session")

    sticky_writer = oni_bridge.ReplayState()
    sticky_writer.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-writer", "seq": 1}
    )
    faulty = make_state("session-writer", 2, snapshot_seq=1)
    faulty["writer"]["fault"] = "append_failed"
    sticky_writer.process(faulty)
    sticky_writer.process(make_state("session-writer", 3, snapshot_seq=2))
    sticky_health = sticky_writer.health(sticky_writer.effective_latest(), {}, replay_mode=False)
    require("writer_fault" in sticky_health["trust_blockers"], "writer fault cleared without a new session")

    sticky_counter_blockers: Dict[str, str] = {}
    for field, blocker in (
        ("fault_count", "writer_faults_detected"),
        ("non_finite_numeric_count", "writer_non_finite_numbers"),
    ):
        session_id = f"session-sticky-{field}"
        counter_runtime = oni_bridge.ReplayState()
        counter_runtime.process(
            {"schema": "oni.event.v2", "event": "session_started", "session_id": session_id, "seq": 1}
        )
        counter_fault = make_state(session_id, 2, snapshot_seq=1)
        counter_fault["writer"][field] = 1
        counter_runtime.process(counter_fault)
        counter_runtime.process(make_state(session_id, 3, snapshot_seq=2))
        counter_health = counter_runtime.health(counter_runtime.effective_latest(), {}, replay_mode=False)
        require(
            blocker in counter_health["trust_blockers"],
            f"{field} cleared inside the producer session",
        )
        sticky_counter_blockers[field] = blocker

    writer_event = oni_bridge.ReplayState()
    writer_event.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-writer-event", "seq": 1}
    )
    writer_event.process(make_state("session-writer-event", 2, snapshot_seq=1))
    writer_event.process(
        {
            "schema": "oni.event.v2",
            "event": "probe_error",
            "session_id": "session-writer-event",
            "seq": 3,
            "stage": "writer_append",
        }
    )
    writer_event_health = writer_event.health(writer_event.effective_latest(), {}, replay_mode=False)
    require("writer_fault" in writer_event_health["trust_blockers"], "writer probe error did not fail closed")

    runtime = oni_bridge.ReplayState()
    runtime.process({"schema": "oni.event.v2", "event": "session_started", "session_id": "session-gap", "seq": 1})
    runtime.process(make_state("session-gap", 3, snapshot_seq=1))
    latest = runtime.effective_latest()
    health = runtime.health(latest, {}, replay_mode=False)
    require(health["status"] == "untrusted_snapshot", "sequence gap did not fail closed")
    require("sequence_gap" in health["trust_blockers"], "sequence gap blocker is missing")
    return {
        "variant_count": len(variants),
        "sequence_gap_status": health["status"],
        "live_statuses": live_statuses,
        "malformed_session_hijack_rejected": True,
        "shared_implementation": True,
        "writer_fault_sticky": True,
        "writer_counter_blockers_sticky": sticky_counter_blockers,
        "writer_probe_error_blocked": True,
        "observed_blockers": observed,
    }


def case_replay_isolation(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True)
    input_path = root / "replay-input.jsonl"
    records: List[Dict[str, Any]] = [
        {"schema": "oni.event.v2", "event": "session_started", "session_id": "session-replay", "seq": 1}
    ]
    for sequence in range(2, 9):
        snapshot = make_state("session-replay", sequence, snapshot_seq=sequence - 1)
        snapshot["alerts"] = ["low_oxygen"]
        snapshot["ts"] = iso_now((sequence - 8) * 5)
        records.append(snapshot)
    input_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in records) + "\n",
        encoding="utf-8",
    )

    baseline = oni_bridge.ReplayState(allow_untrusted_snapshot_baseline=True)
    with mock.patch.object(
        oni_bridge,
        "snapshot_trust_blockers",
        side_effect=AssertionError("baseline replay consulted the current wall clock"),
    ):
        baseline.process_many(copy.deepcopy(records))
    require(
        baseline.latest is not None
        and baseline.latest.get("snapshot_id") == records[-1].get("snapshot_id"),
        "baseline replay did not retain the final prevalidated snapshot",
    )

    isolated = root / "replay-default"
    oni_bridge.run_once(isolated, input_path)
    require(not list((isolated / "commands").rglob("*.json")), "default replay wrote a command")
    health = json.loads((isolated / "health.json").read_text(encoding="utf-8"))
    require(health.get("replay_suggestions_suppressed") is True, "replay suppression was not reported")
    replay_frame = json.loads((isolated / "decision-frame.json").read_text(encoding="utf-8"))
    validate_decision_frame(replay_frame)
    require(
        any(item["code"] == "REPLAY_MODE" for item in replay_frame["blockers"]),
        "canonical frame did not identify replay mode",
    )
    require(not replay_frame["readiness"]["propose"]["ready"], "replay canonical frame allowed proposals")
    replay_packet = json.loads((isolated / "control-packet.json").read_text(encoding="utf-8-sig"))
    require(
        replay_packet.get("next_step") is None
        or replay_packet["next_step"].get("kind") != "action_candidate",
        "default replay control packet selected an action",
    )

    explicit = root / "replay-explicit-sandbox"
    oni_bridge.run_once(explicit, input_path, allow_replay_suggestions=True)
    pending = list((explicit / "commands" / "pending").glob("*.json"))
    require(not pending, "explicit isolated replay created a mutating suggestion")
    explicit_frame = json.loads((explicit / "decision-frame.json").read_text(encoding="utf-8-sig"))
    archived_frame_path = decision_frame_archive_path(
        explicit,
        explicit_frame["frame_id"],
    )
    require(
        archived_frame_path.is_file(),
        "explicit replay did not archive its immutable source frame",
    )
    archived_frame = json.loads(archived_frame_path.read_text(encoding="utf-8-sig"))
    require(
        archived_frame.get("frame_id") == explicit_frame["frame_id"],
        "explicit replay archive identity drifted",
    )
    explicit_packet = json.loads((explicit / "control-packet.json").read_text(encoding="utf-8-sig"))
    require(
        explicit_packet.get("next_step") is None
        or explicit_packet["next_step"].get("kind") != "action_candidate",
        "explicit replay sandbox control packet selected an action",
    )
    explicit_state = json.loads((explicit / "latest_state.json").read_text(encoding="utf-8-sig"))
    low_oxygen_signal = next(
        item
        for item in oni_bridge.agent_rules.evaluate_signals(explicit_state)
        if item.get("incident_id") == "low_oxygen"
    )
    forged_mutation_advice = {
        "incident_id": "low_oxygen",
        "incident_opened_at": records[1]["ts"],
        "risk": low_oxygen_signal["risk"],
        "title": low_oxygen_signal["title"],
        "reason": low_oxygen_signal["reason"],
        "evidence": copy.deepcopy(low_oxygen_signal["evidence"]),
        "source_snapshot_id": explicit_state["snapshot_id"],
        "command": copy.deepcopy(low_oxygen_signal["command"]),
    }
    replay_sink_created = oni_bridge.sync_pending_commands(
        explicit,
        explicit_state,
        [forged_mutation_advice],
        proposal_frame=explicit_frame,
        allow_non_actionable_replay=True,
        policy_override=oni_bridge.load_policy(explicit),
    )
    require(not replay_sink_created, "non-actionable replay sink accepted a mutation")
    require(
        not list((explicit / "commands" / "pending").glob("*.json")),
        "non-actionable replay sink persisted a mutation",
    )

    refused = False
    try:
        oni_bridge.run_once(explicit, input_path, allow_replay_suggestions=True)
    except RuntimeError:
        refused = True
    require(refused, "replay suggestion mode reused a non-isolated output directory")
    return {
        "default_pending": 0,
        "explicit_test_pending": len(pending),
        "source_frame_archived": True,
        "direct_replay_mutation_sink_blocked": True,
        "nonisolated_reuse_refused": refused,
        "baseline_current_wall_clock_calls": 0,
    }


def _control_packet_runtime(session_id: str) -> oni_bridge.ReplayState:
    runtime = oni_bridge.ReplayState()
    runtime.process(
        {"schema": "oni.event.v2", "event": "session_started", "session_id": session_id, "seq": 1}
    )
    runtime.process(make_state(session_id, 2))
    return runtime


def case_control_packet_publication_order_and_crash(root: pathlib.Path) -> Dict[str, Any]:
    output = root / "publication"
    session_id = "session-control-order"
    runtime = _control_packet_runtime(session_id)
    events: List[str] = []
    policy_ids: Dict[str, int] = {}

    original_atomic = oni_bridge.atomic_write_json
    original_gate = oni_bridge.command_pipeline_readiness
    original_archive = oni_bridge.persist_decision_frame_archive
    original_sync = oni_bridge.sync_pending_commands
    original_prune = oni_bridge.prune_decision_frame_archives
    session_dir = output / "sessions" / session_id

    def tracked_atomic(path: pathlib.Path, payload: Any) -> None:
        resolved = path.resolve()
        if resolved == (output / "decision-frame.json").resolve():
            events.append("root_frame")
        elif resolved == (session_dir / "decision-frame.json").resolve():
            events.append("session_frame")
        elif resolved == (session_dir / "control-packet.json").resolve():
            events.append("session_packet")
        elif resolved == (output / "control-packet.json").resolve():
            events.append("root_packet")
        original_atomic(path, payload)

    def tracked_gate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        proposal_frame = kwargs.get("proposal_frame")
        label = "post_gate" if isinstance(proposal_frame, dict) else "pre_gate"
        events.append(label)
        if isinstance(kwargs.get("policy"), dict):
            policy_ids[label] = id(kwargs["policy"])
        return original_gate(*args, **kwargs)

    def tracked_archive(*args: Any, **kwargs: Any) -> Any:
        events.append("archive")
        return original_archive(*args, **kwargs)

    def tracked_sync(*args: Any, **kwargs: Any) -> Any:
        events.append("sync")
        policy_override = kwargs.get("policy_override")
        require(isinstance(policy_override, dict), "Bridge did not pass the loaded policy override")
        policy_ids["sync"] = id(policy_override)
        return original_sync(*args, **kwargs)

    def tracked_prune(*args: Any, **kwargs: Any) -> Any:
        events.append("prune")
        return original_prune(*args, **kwargs)

    with (
        mock.patch.object(oni_bridge, "atomic_write_json", side_effect=tracked_atomic),
        mock.patch.object(oni_bridge, "command_pipeline_readiness", side_effect=tracked_gate),
        mock.patch.object(oni_bridge, "persist_decision_frame_archive", side_effect=tracked_archive),
        mock.patch.object(oni_bridge, "sync_pending_commands", side_effect=tracked_sync),
        mock.patch.object(oni_bridge, "prune_decision_frame_archives", side_effect=tracked_prune),
    ):
        oni_bridge.write_outputs(output, runtime)

    required_order = [
        "pre_gate",
        "root_frame",
        "session_frame",
        "archive",
        "sync",
        "post_gate",
        "session_packet",
        "root_packet",
        "prune",
    ]
    positions = [events.index(name) for name in required_order]
    require(positions == sorted(positions), f"control packet publication order drifted: {events!r}")
    require(
        policy_ids.get("pre_gate") == policy_ids.get("sync") == policy_ids.get("post_gate"),
        "Bridge reloaded or replaced policy between frame, sync, and post-sync gate",
    )

    root_packet_path = output / "control-packet.json"
    session_packet_path = session_dir / "control-packet.json"
    root_packet = json.loads(root_packet_path.read_text(encoding="utf-8-sig"))
    session_packet = json.loads(session_packet_path.read_text(encoding="utf-8-sig"))
    require(root_packet == session_packet, "root and session packet publications disagree")
    committed_frame = json.loads((output / "decision-frame.json").read_text(encoding="utf-8-sig"))
    current_policy = oni_bridge.load_policy(output)
    current_gate = original_gate(
        output,
        latest_state=runtime.effective_latest(),
        proposal_frame=committed_frame,
        policy=current_policy,
    )
    validate_control_packet(root_packet, committed_frame, current_gate)

    old_packet_bytes = root_packet_path.read_bytes()
    old_packet = copy.deepcopy(root_packet)
    runtime.process(make_state(session_id, 3))

    def crash_post_sync_gate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        if isinstance(kwargs.get("proposal_frame"), dict):
            raise OSError(errno.EIO, "synthetic post-sync gate crash")
        return original_gate(*args, **kwargs)

    crashed = False
    try:
        with mock.patch.object(
            oni_bridge, "command_pipeline_readiness", side_effect=crash_post_sync_gate
        ):
            oni_bridge.write_outputs(output, runtime)
    except OSError:
        crashed = True
    require(crashed, "post-sync gate crash fixture did not fire")
    require(root_packet_path.read_bytes() == old_packet_bytes, "root packet changed after gate failure")
    new_frame = json.loads((output / "decision-frame.json").read_text(encoding="utf-8-sig"))
    require(old_packet["source"]["frame_id"] != new_frame["frame_id"], "crash did not advance the frame")
    new_gate = original_gate(
        output,
        latest_state=runtime.effective_latest(),
        proposal_frame=new_frame,
        policy=oni_bridge.load_policy(output),
    )
    mismatch_rejected = False
    try:
        validate_control_packet(old_packet, new_frame, new_gate)
    except ControlPacketError:
        mismatch_rejected = True
    require(mismatch_rejected, "old packet remained valid against the newly committed frame")
    return {
        "publication_order": required_order,
        "same_policy_object": True,
        "old_packet_new_frame_mismatch_rejected": True,
    }


def case_control_packet_session_and_policy_isolation(root: pathlib.Path) -> Dict[str, Any]:
    output = root / "isolation"
    runtime = _control_packet_runtime("session-control-a")
    oni_bridge.write_outputs(output, runtime)
    first_path = output / "sessions" / "session-control-a" / "control-packet.json"
    first_bytes = first_path.read_bytes()
    first_packet = json.loads(first_bytes.decode("utf-8-sig"))

    runtime.process(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": "session-control-b",
            "seq": 1,
        }
    )
    runtime.process(make_state("session-control-b", 2))
    oni_bridge.write_outputs(output, runtime)
    second_path = output / "sessions" / "session-control-b" / "control-packet.json"
    second_packet = json.loads(second_path.read_text(encoding="utf-8-sig"))
    root_packet = json.loads((output / "control-packet.json").read_text(encoding="utf-8-sig"))
    require(first_path.read_bytes() == first_bytes, "new session rewrote the old session packet")
    require(first_packet["source"]["session_id"] == "session-control-a", "session A packet identity drifted")
    require(second_packet["source"]["session_id"] == "session-control-b", "session B packet identity drifted")
    require(root_packet == second_packet, "root marker did not identify the current session packet")

    disabled = root / "disabled"
    policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_actions": [],
        "allowed_save_hashes": [],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
    }
    oni_bridge.atomic_write_json(disabled / "action-policy.json", policy)
    disabled_runtime = _control_packet_runtime("session-control-disabled")
    oni_bridge.write_outputs(disabled, disabled_runtime)
    disabled_packet = json.loads((disabled / "control-packet.json").read_text(encoding="utf-8-sig"))
    require(
        disabled_packet.get("next_step") is None
        or disabled_packet["next_step"].get("kind") not in {"action_candidate", "operator_recommendation"},
        "disabled suggestions produced an action/recommendation",
    )

    candidate_output = root / "candidate"
    candidate_policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": ["set_paused"],
        "allowed_save_hashes": ["sha256:" + "a" * 64],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
    }
    oni_bridge.atomic_write_json(candidate_output / "action-policy.json", candidate_policy)
    candidate_runtime = oni_bridge.ReplayState()
    candidate_runtime.process(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": "session-control-candidate",
            "seq": 1,
        }
    )
    for sequence in range(2, 9):
        snapshot = make_state(
            "session-control-candidate",
            sequence,
            timestamp=iso_now((sequence - 8) * 5),
            snapshot_seq=sequence - 1,
        )
        snapshot["alerts"] = ["low_oxygen"]
        # Model seven genuinely fresh publications over a 30-second window.
        # The verifier executes them synchronously, so pin only the admission
        # clock for each sample to that sample's publication time.  Production
        # freshness and the low-oxygen 7-sample/30-second hysteresis remain
        # unchanged; the final sample is still fresh against the real clock
        # used by write_outputs below.
        observed_at = dt.datetime.fromisoformat(snapshot["ts"])
        with (
            mock.patch.object(
                oni_bridge,
                "snapshot_trust_blockers",
                side_effect=lambda value, now=None, clock=observed_at: state_quality.snapshot_trust_blockers(
                    value, now=now or clock
                ),
            ),
            mock.patch.object(
                oni_bridge,
                "snapshot_age_seconds",
                side_effect=lambda value, now=None, clock=observed_at: state_quality.snapshot_age_seconds(
                    value, now=now or clock
                ),
            ),
        ):
            candidate_runtime.process(snapshot)
    oni_bridge.write_outputs(candidate_output, candidate_runtime)
    candidate_packet = json.loads(
        (candidate_output / "control-packet.json").read_text(encoding="utf-8-sig")
    )
    action_candidate = candidate_packet.get("next_step") or {}
    require(action_candidate.get("kind") == "action_candidate", "Bridge did not publish the unique current candidate")
    require(
        set(action_candidate)
        == {"kind", "command_id", "action", "intent_hash", "risk", "requires_approval", "auto_execute"},
        "Bridge action candidate leaked command material",
    )
    require(
        not ({"args", "path", "body", "preconditions", "expected_effects"} & set(action_candidate)),
        "Bridge action candidate exposed executable fields",
    )
    return {
        "session_a_immutable": True,
        "root_tracks_session_b": True,
        "disabled_suggestions_fail_closed": True,
        "unique_current_candidate_published": True,
    }


def case_control_packet_post_sync_read_error(root: pathlib.Path) -> Dict[str, Any]:
    output = root / "post-sync-read-error"
    pending = output / "commands" / "pending" / "read-error.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(
        json.dumps(
            {
                "id": "cmd-read-error",
                "action": "focus_note",
                "mode": "suggested",
                "status": "pending_human_approval",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    runtime = _control_packet_runtime("session-control-read-error")
    original_gate = oni_bridge.command_pipeline_readiness
    original_read_text = pathlib.Path.read_text
    captured: Dict[str, Any] = {}

    def fail_target_read(path: pathlib.Path, *args: Any, **kwargs: Any) -> str:
        if path.resolve() == pending.resolve():
            raise OSError(errno.EIO, "synthetic post-sync read error")
        return original_read_text(path, *args, **kwargs)

    def post_sync_error_gate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        if isinstance(kwargs.get("proposal_frame"), dict):
            with mock.patch.object(pathlib.Path, "read_text", new=fail_target_read):
                value = original_gate(*args, **kwargs)
            captured["gate"] = value
            return value
        return original_gate(*args, **kwargs)

    with mock.patch.object(
        oni_bridge, "command_pipeline_readiness", side_effect=post_sync_error_gate
    ):
        oni_bridge.write_outputs(output, runtime, replay_mode=True)

    packet = json.loads((output / "control-packet.json").read_text(encoding="utf-8-sig"))
    source_frame = json.loads((output / "decision-frame.json").read_text(encoding="utf-8-sig"))
    require(captured.get("gate", {}).get("invalid_records"), "post-sync read error was not structured")
    require(packet.get("next_step") is None, "structured post-sync read error did not publish null")
    validate_control_packet(packet, source_frame, captured["gate"])
    return {"structured_invalid_read": True, "null_packet_published": True}


def case_rotation(root: pathlib.Path) -> Dict[str, Any]:
    directory = root / "rotation"
    directory.mkdir(parents=True)
    log = directory / "state.jsonl"
    log.write_bytes(b"x" * 2048)
    pathlib.Path(str(log) + ".1").write_bytes(b"backup-one")
    pathlib.Path(str(log) + ".2").write_bytes(b"backup-two")
    original_limit = oni_bridge.MAX_LOG_BYTES
    try:
        oni_bridge.MAX_LOG_BYTES = 1024
        require(oni_bridge.rotate_log(log), "oversized state log did not rotate")
    finally:
        oni_bridge.MAX_LOG_BYTES = original_limit

    require(not log.exists(), "rotated source log still exists")
    require(pathlib.Path(str(log) + ".1").stat().st_size == 2048, "newest rotated log is wrong")
    require(pathlib.Path(str(log) + ".2").read_bytes() == b"backup-one", "first backup was not shifted")
    require(pathlib.Path(str(log) + ".3").read_bytes() == b"backup-two", "second backup was not shifted")
    return {"configured_limit_bytes": original_limit, "backup_count": oni_bridge.LOG_BACKUPS}


def case_rotation_handoff(root: pathlib.Path) -> Dict[str, Any]:
    """Exercise the cursor handoff when a writer races log rotation.

    The fixture uses approximately the 3.0 MiB state-record size observed in
    run17 and a fixed 20 MiB threshold, so rotation is deterministic rather
    than timing-dependent.
    """
    line_blob = "x" * 3_000_000
    line_bytes = len(json_line({**make_state("offline-rotation", 1), "fixture_blob": line_blob})) + 1
    limit = 20 * 1024 * 1024

    def record(seq: int, session: str = "offline-rotation") -> Dict[str, Any]:
        value = make_state(session, seq, snapshot_seq=seq)
        value["fixture_blob"] = line_blob
        return value

    def prepare_log(directory: pathlib.Path, *, tail: List[Dict[str, Any]]) -> tuple[pathlib.Path, int]:
        log = directory / "state.jsonl"
        log.write_bytes(b"".join(json_line(record(seq)) + b"\n" for seq in range(1, 8)))
        _initial, position, _invalid, _partial = oni_bridge.read_new_records(log, 0)
        with log.open("ab") as handle:
            for value in tail:
                handle.write(json_line(value) + b"\n")
        original_limit = oni_bridge.MAX_LOG_BYTES
        oni_bridge.MAX_LOG_BYTES = limit
        try:
            require(oni_bridge.rotate_log(log), "deterministic rotation did not occur")
        finally:
            oni_bridge.MAX_LOG_BYTES = original_limit
        return log, position

    # The pre-fix race: the tail moved to .1, while the next read looked only
    # at the newly-created active path and therefore observed nothing.
    repro_dir = root / "rotation-handoff-repro"
    repro_dir.mkdir(parents=True)
    repro_log, repro_position = prepare_log(repro_dir, tail=[record(8)])
    unpatched_records, _position, _invalid, _partial = oni_bridge.read_new_records(
        repro_log, repro_position
    )
    require(not unpatched_records, "unpatched race fixture unexpectedly recovered moved tail")

    # Fixed path: consume the moved tail from the pre-rotation cursor, then
    # consume the new active file.  The runtime must see 7 -> 8 -> 9.
    fixed_dir = root / "rotation-handoff-fixed"
    fixed_dir.mkdir(parents=True)
    fixed_log, fixed_position = prepare_log(fixed_dir, tail=[record(8)])
    moved, _moved_position, moved_invalid, moved_partial = oni_bridge.read_rotated_tail(
        fixed_log, fixed_position
    )
    with fixed_log.open("ab") as handle:
        handle.write(json_line(record(9)) + b"\n")
    current, _current_position, current_invalid, current_partial = oni_bridge.read_new_records(
        fixed_log, 0
    )
    runtime = oni_bridge.ReplayState()
    runtime.process_many([record(seq) for seq in range(1, 8)])
    runtime.process_many(moved + current)
    require([item["seq"] for item in moved] == [8], "moved tail was not recovered")
    require([item["seq"] for item in current] == [9], "new active record was not read")
    require(runtime.seq_gap_count == 0, "continuous rotation introduced a sequence gap")

    # A real missing record must remain fail-closed after the handoff.
    missing_dir = root / "rotation-handoff-missing"
    missing_dir.mkdir(parents=True)
    missing_log, missing_position = prepare_log(missing_dir, tail=[])
    missing_moved, *_ = oni_bridge.read_rotated_tail(missing_log, missing_position)
    with missing_log.open("ab") as handle:
        handle.write(json_line(record(9)) + b"\n")
    missing_current, *_ = oni_bridge.read_new_records(missing_log, 0)
    missing_runtime = oni_bridge.ReplayState()
    missing_runtime.process_many([record(seq) for seq in range(1, 8)])
    missing_runtime.process_many(missing_moved + missing_current)
    require(missing_runtime.seq_gap_count == 1, "a genuine missing record was masked")

    # Boundary protocol checks: duplicate records stay duplicates, incomplete
    # lines stay unparsed, and a new session resets the sequence contract.
    boundary_dir = root / "rotation-handoff-boundaries"
    boundary_dir.mkdir(parents=True)
    boundary_log, boundary_position = prepare_log(boundary_dir, tail=[record(8)])
    with pathlib.Path(str(boundary_log) + ".1").open("ab") as handle:
        handle.write(json_line(record(8)) + b"\n")
        handle.write(b"{\"schema\":\"oni.state.v2\"")
    boundary_moved, _, _, boundary_partial = oni_bridge.read_rotated_tail(boundary_log, boundary_position)
    boundary_runtime = oni_bridge.ReplayState()
    boundary_runtime.process_many([record(seq) for seq in range(1, 8)])
    boundary_runtime.process_many(boundary_moved)
    require(boundary_partial > 0, "incomplete boundary line was not reported")
    require(boundary_runtime.duplicate_seq_count == 1, "boundary duplicate was not counted")
    new_session = {
        "schema": "oni.event.v2",
        "event": "session_started",
        "session_id": "offline-rotation-next",
        "seq": 1,
    }
    boundary_runtime.process(new_session)
    boundary_runtime.process(record(2, session="offline-rotation-next"))
    require(boundary_runtime.current_session_id == "offline-rotation-next", "cross-session handoff was rejected")
    require(boundary_runtime.seq_gap_count == 0, "cross-session sequence was compared to retired session")

    return {
        "record_bytes": line_bytes,
        "rotation_limit_bytes": limit,
        "unpatched_moved_tail_records": len(unpatched_records),
        "fixed_moved_tail_sequences": [item["seq"] for item in moved],
        "fixed_active_sequences": [item["seq"] for item in current],
        "missing_record_gap_count": missing_runtime.seq_gap_count,
        "boundary_duplicate_count": boundary_runtime.duplicate_seq_count,
        "boundary_partial_bytes": boundary_partial,
        "cross_session_current": boundary_runtime.current_session_id,
        "invalid_records": moved_invalid + current_invalid,
        "partial_records": moved_partial + current_partial,
    }


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_peak_working_set(process: subprocess.Popen[str]) -> int:
    if os.name != "nt":
        return 0
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    success = ctypes.windll.psapi.GetProcessMemoryInfo(
        int(process._handle),  # type: ignore[attr-defined]
        ctypes.byref(counters),
        counters.cb,
    )
    return int(counters.PeakWorkingSetSize) if success else 0


def case_large_replay_budget(root: pathlib.Path) -> Dict[str, Any]:
    directory = root / "large-replay"
    directory.mkdir(parents=True)
    expected = startup_expectations()
    old_source = directory / "input-old-scope.jsonl"
    old_state = make_state("session-performance", 1)
    with old_source.open("wb") as handle:
        for seq in range(1, PERF_LINE_COUNT + 1):
            old_state["seq"] = seq
            old_state["snapshot_seq"] = seq
            old_state["snapshot_id"] = f"session-performance:{seq}"
            handle.write(json_line(old_state) + b"\n")

    bound_source = directory / "input-startup-bound.jsonl"
    bound_state = startup_snapshot(
        expected,
        "session-performance",
        2,
        1,
        active_path_id=expected["loader_path_id_upper"],
        save_id_hash=expected["loader_save_sha256"],
    )
    with bound_source.open("wb") as handle:
        handle.write(
            json_line(
                startup_event(
                    expected,
                    "session-performance",
                    1,
                    "session_started",
                    active_path_id=expected["loader_path_id_upper"],
                    save_id_hash=expected["loader_save_sha256"],
                    reason="save_loader_load_complete",
                )
            )
            + b"\n"
        )
        for seq in range(2, PERF_LINE_COUNT + 1):
            bound_state["seq"] = seq
            bound_state["snapshot_seq"] = seq - 1
            bound_state["snapshot_id"] = f"session-performance:{seq}"
            handle.write(json_line(bound_state) + b"\n")

    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def measure(
        name: str,
        command: List[str],
        *,
        budget_seconds: float,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        peak_working_set = 0
        while process.poll() is None:
            peak_working_set = max(peak_working_set, process_peak_working_set(process))
            if time.perf_counter() - started > 20:
                process.kill()
                raise AssertionError(f"{name} exceeded the hard test timeout")
            time.sleep(0.005)
        peak_working_set = max(peak_working_set, process_peak_working_set(process))
        stdout, stderr = process.communicate()
        elapsed = time.perf_counter() - started
        require(process.returncode == 0, f"{name} failed: {(stderr or stdout)[-500:]}")
        require(elapsed <= budget_seconds, f"{name} took {elapsed:.3f}s")
        require(peak_working_set > 0, f"{name} peak working set could not be measured")
        require(
            peak_working_set <= MAX_PEAK_WORKING_SET,
            f"{name} peak working set was {peak_working_set / 1048576:.1f} MiB",
        )
        return {
            "elapsed_seconds": round(elapsed, 3),
            "peak_working_set_mib": round(peak_working_set / 1048576, 3),
            "budget_seconds": budget_seconds,
            "budget_peak_mib": MAX_PEAK_WORKING_SET / 1048576,
        }

    old_output = directory / "old-scope-output"
    old_metrics = measure(
        "10,000-line original --input replay",
        [
            sys.executable,
            str(PROJECT_ROOT / "bridge" / "oni_bridge.py"),
            "--bridge-dir",
            str(old_output),
            "--input",
            str(old_source),
            "--once",
        ],
        budget_seconds=MAX_REPLAY_SECONDS,
    )

    deferred_output = directory / "startup-bound-deferred-output"
    deferred_metrics = measure(
        "10,000-line startup-bound deferred-strategy replay",
        [
            sys.executable,
            "-c",
            (
                "import json,pathlib,sys;"
                "from bridge import oni_bridge as b;"
                "source=pathlib.Path(sys.argv[1]);output=pathlib.Path(sys.argv[2]);"
                "expected=json.loads(sys.argv[3]);"
                "runtime,_=b.replay_initial_records("
                "source,output,allow_untrusted_snapshot_baseline=True,"
                "startup_expectations=expected,require_startup_binding=True);"
                "b.write_outputs(output,runtime,replay_mode=True)"
            ),
            str(bound_source),
            str(deferred_output),
            json.dumps(expected, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        ],
        budget_seconds=MAX_REPLAY_SECONDS,
    )

    live_output = directory / "startup-bound-live-output"
    live_output.mkdir()
    # Earlier replay modes must not consume this timed run's freshness budget.
    # Re-emit the exact fixture with a fresh publication timestamp, retaining
    # every startup anchor, sequence and snapshot. Never bypass the live clock.
    live_timestamp = iso_now()
    with bound_source.open("rb") as source, (live_output / "state.jsonl").open("wb") as target:
        for line in source:
            record = json.loads(line)
            record["ts"] = live_timestamp
            target.write(json_line(record) + b"\n")
    live_counts_path = directory / "live-call-counts.json"
    live_metrics = measure(
        "10,000-line startup-bound live initial replay",
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "measure_bridge_replay.py"),
            str(live_counts_path),
            "--bridge-dir",
            str(live_output),
            "--once",
            "--expected-validation-run-id",
            expected["validation_run_id"],
            "--expected-validation-process-id",
            str(expected["validation_process_id"]),
            "--expected-startup-binding-sha256",
            expected["startup_binding_hash"],
            "--expected-startup-intent-hash",
            expected["startup_intent_hash"],
            "--expected-runtime-config-evidence-sha256",
            expected["runtime_config_evidence_sha256"],
            "--expected-loader-path-id-upper",
            expected["loader_path_id_upper"],
            "--expected-loader-save-sha256",
            expected["loader_save_sha256"],
            "--expected-game-build",
            expected["game_build"],
            "--expected-mod-version",
            expected["mod_version"],
        ],
        budget_seconds=MAX_LIVE_INITIAL_REPLAY_SECONDS,
    )
    live_counts = json.loads(live_counts_path.read_text(encoding="utf-8"))["counts"]
    require(live_counts["process"] == PERF_LINE_COUNT, "live replay skipped event processing")
    # 9,999 intermediate snapshots plus the final output projection.
    require(live_counts["evaluate_strategy"] == PERF_LINE_COUNT, "live replay skipped strategy evaluations")
    live_metrics["call_counts"] = live_counts

    for output, startup_bound in (
        (old_output, False),
        (deferred_output, True),
        (live_output, True),
    ):
        latest = json.loads((output / "latest_state.json").read_text(encoding="utf-8"))
        health = json.loads((output / "health.json").read_text(encoding="utf-8"))
        if output == live_output:
            require(health["status"] == "ok", "live replay ended with stale or untrusted state")
        require(latest["seq"] == PERF_LINE_COUNT, "large replay did not retain the final snapshot")
        if startup_bound:
            require(
                health["startup_binding"]["verified"]
                and health["startup_binding"]["current_save_id_hash"]
                == expected["loader_save_sha256"],
                "startup-bound replay did not retain its external binding",
            )
        session_log = output / "sessions" / "session-performance" / "state.jsonl"
        require(session_log.exists(), "session-partitioned state log was not written")
        require(
            sum(1 for _ in session_log.open("r", encoding="utf-8")) == PERF_LINE_COUNT,
            "session state log line count is wrong",
        )
        require(not list(output.rglob("*.tmp")), "large replay left atomic-write temporary files")
        summary = (output / "state_summary.md").read_text(encoding="utf-8")
        require(
            len(summary.encode("utf-8")) <= oni_bridge.MAX_SUMMARY_BYTES,
            "large replay summary exceeded 8 KiB",
        )
        require(
            len(summary.splitlines()) <= oni_bridge.MAX_SUMMARY_LINES,
            "large replay summary exceeded 100 lines",
        )

    return {
        "line_count": PERF_LINE_COUNT,
        "original_scope_input_bytes": old_source.stat().st_size,
        "startup_bound_input_bytes": bound_source.stat().st_size,
        "original_scope": old_metrics,
        "startup_bound_deferred_strategy": deferred_metrics,
        "startup_bound_live_initial_replay": live_metrics,
        "observed_live_minus_deferred_seconds": round(
            live_metrics["elapsed_seconds"] - deferred_metrics["elapsed_seconds"],
            3,
        ),
    }


def run_case(name: str, function: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function(root / name)
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "classification": "behavior_fail",
            "error": str(exc),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=PROJECT_ROOT / "reports" / "bridge-reliability.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases: List[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("atomic_replace_retries", case_atomic_replace_retries),
        ("archive_identity_noop_fail_closed", case_archive_identity_noop_fail_closed),
        ("partial_bad_truncated", case_partial_bad_and_truncated),
        ("sequence_session_isolation", case_sequence_and_session_isolation),
        ("stale_atomic_outputs", case_stale_and_atomic_outputs),
        ("health_publication_clock", case_health_publication_clock),
        ("external_startup_binding_gate", case_external_startup_binding_gate),
        ("authorized_derivative_transition", case_authorized_derivative_transition),
        ("strict_snapshot_admission", case_strict_snapshot_admission),
        ("replay_isolation", case_replay_isolation),
        ("control_packet_publication_order_and_crash", case_control_packet_publication_order_and_crash),
        ("control_packet_session_and_policy_isolation", case_control_packet_session_and_policy_isolation),
        ("control_packet_post_sync_read_error", case_control_packet_post_sync_read_error),
        ("rotation", case_rotation),
        ("rotation_handoff", case_rotation_handoff),
        ("large_replay_budget", case_large_replay_budget),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-bridge-v2-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.bridge-reliability-report.v1",
        "generated_at": iso_now(),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "scenario_count": len(cases),
        "assertion_count": ASSERTIONS,
        "cases": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), ensure_ascii=False, sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Scenarios: {len(cases)}; assertions: {ASSERTIONS}")
    print(f"Report: {args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
