#!/usr/bin/env python3
"""Compare two offline ONI safety-boundary reports exactly and fail closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import stat
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Sequence


SCHEMA = "oni.live-safety-boundary.v1"
REPORT_SCHEMA = "oni.live-safety-boundary-comparison.v1"
MAX_REPORT_BYTES = 16 * 1024 * 1024
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
QUEUE_STAGES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
    "done",
    "failed",
    "rejected",
    "quarantine",
    "temporary_files",
)
SECTION_SCOPES = {
    "saves": {"save_files", "cloud_save_files"},
    "mod_configs": {"mod_config", "player_prefs"},
    "installed_probe": {"installed_probe"},
    "bridge_policies": {"bridge_policy"},
}


def reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError(f"{label} is not a canonical SHA-256 identifier")
    return value


def require_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a UTC offset")
    return value


def load_report(path: pathlib.Path, label: str) -> Dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} report is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} report must be a regular non-symlink file")
    if metadata.st_size > MAX_REPORT_BYTES:
        raise ValueError(f"{label} report exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} report could not be read as UTF-8") from exc
    value = json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"{label} report has an unsupported schema")
    validate_report(value, label)
    return value


def validate_report(report: Mapping[str, Any], label: str) -> None:
    required_top = {
        "schema",
        "run_id",
        "phase",
        "captured_at",
        "oni_root_id",
        "path_disclosure",
        "saves",
        "mod_configs",
        "installed_probe",
        "bridge_policies",
        "queues",
        "processes",
    }
    if set(report) != required_top:
        raise ValueError(f"{label} report fields do not match the schema")
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"{label} run_id is invalid")
    if report.get("phase") not in {"pre", "post", "checkpoint"}:
        raise ValueError(f"{label} phase is invalid")
    require_timestamp(report.get("captured_at"), f"{label} captured_at")
    require_hash(report.get("oni_root_id"), f"{label} oni_root_id")
    if report.get("path_disclosure") != "identifiers_only":
        raise ValueError(f"{label} path disclosure mode is invalid")

    for section in SECTION_SCOPES:
        entries(report, section, label)

    queues = report.get("queues")
    if not isinstance(queues, Mapping) or set(queues) != set(QUEUE_STAGES):
        raise ValueError(f"{label} queue counters do not match the schema")
    for stage in QUEUE_STAGES:
        if not is_nonnegative_int(queues.get(stage)):
            raise ValueError(f"{label} queue counter {stage} is invalid")

    processes = report.get("processes")
    if not isinstance(processes, Mapping) or set(processes) != {
        "oxygen_not_included_count",
        "bridge_count",
    }:
        raise ValueError(f"{label} process counters do not match the schema")
    for name in ("oxygen_not_included_count", "bridge_count"):
        if not is_nonnegative_int(processes.get(name)):
            raise ValueError(f"{label} process counter {name} is invalid")


def entries(
    report: Mapping[str, Any], section: str, label: str = "report"
) -> list[Dict[str, Any]]:
    container = report.get(section)
    if not isinstance(container, Mapping) or set(container) != {"count", "entries"}:
        raise ValueError(f"{label} {section} container is invalid")
    raw_entries = container.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{label} {section} entries are missing")
    if not is_nonnegative_int(container.get("count")) or container.get("count") != len(
        raw_entries
    ):
        raise ValueError(f"{label} {section} count does not match entries")

    common_fields = {
        "scope",
        "path_id",
        "parent_path_id",
        "file_name_id",
        "extension",
        "length",
        "last_write_utc",
        "sha256",
    }
    expected_fields = common_fields | ({"stem_id"} if section == "saves" else set())
    result: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_entries):
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ValueError(f"{label} {section} entry {index} fields are invalid")
        if item.get("scope") not in SECTION_SCOPES[section]:
            raise ValueError(f"{label} {section} entry {index} scope is invalid")
        path_id = require_hash(item.get("path_id"), f"{label} {section} path_id")
        if path_id in seen:
            raise ValueError(f"{label} {section} contains a duplicate path_id")
        seen.add(path_id)
        require_hash(item.get("parent_path_id"), f"{label} {section} parent_path_id")
        require_hash(item.get("file_name_id"), f"{label} {section} file_name_id")
        extension = item.get("extension")
        if (
            not isinstance(extension, str)
            or extension != extension.lower()
            or not extension.startswith(".")
            or len(extension) > 16
        ):
            raise ValueError(f"{label} {section} extension is invalid")
        if section == "saves":
            if extension not in {".sav", ".png"}:
                raise ValueError(f"{label} saves entry extension is invalid")
            require_hash(item.get("stem_id"), f"{label} saves stem_id")
        if not is_nonnegative_int(item.get("length")):
            raise ValueError(f"{label} {section} length is invalid")
        require_timestamp(item.get("last_write_utc"), f"{label} {section} last_write_utc")
        require_hash(item.get("sha256"), f"{label} {section} sha256")
        result.append(item)
    return result


def by_path_id(items: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {str(item["path_id"]): item for item in items}


def compare_exact(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    before_index = by_path_id(before)
    after_index = by_path_id(after)
    missing = sorted(path_id for path_id in before_index if path_id not in after_index)
    changed = sorted(
        path_id
        for path_id in before_index.keys() & after_index.keys()
        if before_index[path_id] != after_index[path_id]
    )
    added = sorted(path_id for path_id in after_index if path_id not in before_index)
    return {"missing": missing, "changed": changed, "added": added}


def validate_allowed_stem(value: str) -> str:
    if not STEM_RE.fullmatch(value) or value.endswith("."):
        raise argparse.ArgumentTypeError("allowed save stem is not Windows-safe")
    if value.split(".", 1)[0].lower() in RESERVED_WINDOWS_NAMES:
        raise argparse.ArgumentTypeError("allowed save stem uses a reserved device name")
    return value


def stem_id(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.lower().encode("utf-8")).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre", type=pathlib.Path, required=True)
    parser.add_argument("--post", type=pathlib.Path, required=True)
    parser.add_argument(
        "--allow-new-save-stem",
        action="append",
        default=[],
        type=validate_allowed_stem,
        help="Allow one newly-created .sav/.png sibling pair with this exact stem.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        before = load_report(args.pre, "pre")
        after = load_report(args.post, "post")
        if before.get("phase") != "pre" or after.get("phase") != "post":
            raise ValueError("comparison requires pre and post phase reports")
        if before.get("run_id") != after.get("run_id"):
            raise ValueError("pre/post run_id mismatch")
        if before.get("oni_root_id") != after.get("oni_root_id"):
            raise ValueError("pre/post ONI root identity mismatch")

        save_before = entries(before, "saves", "pre")
        save_after = entries(after, "saves", "post")
        save_diff = compare_exact(save_before, save_after)
        after_save_index = by_path_id(save_after)
        allowed_stem_ids = {stem_id(value) for value in args.allow_new_save_stem}
        unexpected_added_saves: list[Dict[str, Any]] = []
        allowed_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for path_id in save_diff["added"]:
            item = after_save_index[path_id]
            item_stem_id = str(item["stem_id"])
            if item_stem_id not in allowed_stem_ids:
                unexpected_added_saves.append(
                    {
                        "path_id": path_id,
                        "reason": "stem_not_allowed",
                    }
                )
                continue
            key = (
                str(item["scope"]),
                str(item["parent_path_id"]),
                item_stem_id,
            )
            allowed_groups[key].append(item)

        incomplete_allowed_pairs: list[Dict[str, Any]] = []
        group_count_by_stem: dict[str, int] = defaultdict(int)
        for (scope, parent_path_id, item_stem_id), grouped in sorted(allowed_groups.items()):
            group_count_by_stem[item_stem_id] += 1
            extensions = sorted(str(item["extension"]) for item in grouped)
            if extensions != [".png", ".sav"]:
                incomplete_allowed_pairs.append(
                    {
                        "scope": scope,
                        "parent_path_id": parent_path_id,
                        "stem_id": item_stem_id,
                        "extensions": extensions,
                    }
                )
        for item_stem_id, group_count in sorted(group_count_by_stem.items()):
            if group_count > 1:
                incomplete_allowed_pairs.append(
                    {
                        "stem_id": item_stem_id,
                        "reason": "multiple_sibling_pairs_for_one_allowed_stem",
                        "group_count": group_count,
                    }
                )

        config_diff = compare_exact(
            entries(before, "mod_configs", "pre"),
            entries(after, "mod_configs", "post"),
        )
        probe_diff = compare_exact(
            entries(before, "installed_probe", "pre"),
            entries(after, "installed_probe", "post"),
        )
        policy_diff = compare_exact(
            entries(before, "bridge_policies", "pre"),
            entries(after, "bridge_policies", "post"),
        )

        queues = after["queues"]
        nonterminal = {
            stage: int(queues[stage])
            for stage in ("pending", "approving", "approved", "executing", "confirming")
        }
        processes = after["processes"]
        blockers: list[str] = []
        if save_diff["missing"]:
            blockers.append("preexisting_save_missing")
        if save_diff["changed"]:
            blockers.append("preexisting_save_changed")
        if unexpected_added_saves:
            blockers.append("unexpected_save_added")
        if incomplete_allowed_pairs:
            blockers.append("allowed_save_pair_incomplete_or_ambiguous")
        if config_diff["missing"] or config_diff["changed"] or config_diff["added"]:
            blockers.append("mod_or_player_config_not_restored")
        if probe_diff["missing"] or probe_diff["changed"] or probe_diff["added"]:
            blockers.append("installed_probe_not_restored")
        if policy_diff["missing"] or policy_diff["changed"] or policy_diff["added"]:
            blockers.append("bridge_policy_not_restored")
        if any(nonterminal.values()):
            blockers.append("nonterminal_command_queue_not_empty")
        if int(queues["temporary_files"]):
            blockers.append("command_temporary_files_present")
        if int(processes["oxygen_not_included_count"]):
            blockers.append("game_process_still_running")
        if int(processes["bridge_count"]):
            blockers.append("bridge_process_still_running")

        payload = {
            "schema": REPORT_SCHEMA,
            "status": "passed" if not blockers else "blocked",
            "run_id": before["run_id"],
            "blockers": blockers,
            "preexisting_save_count": len(save_before),
            "new_save_count": len(save_diff["added"]),
            "unexpected_added_saves": unexpected_added_saves,
            "incomplete_allowed_pairs": incomplete_allowed_pairs,
            "save_diff": save_diff,
            "mod_config_diff": config_diff,
            "installed_probe_diff": probe_diff,
            "bridge_policy_diff": policy_diff,
            "nonterminal_queues": nonterminal,
            "temporary_file_count": int(queues["temporary_files"]),
            "processes": {
                "oxygen_not_included_count": int(processes["oxygen_not_included_count"]),
                "bridge_count": int(processes["bridge_count"]),
            },
        }
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        payload = {
            "schema": REPORT_SCHEMA,
            "status": "blocked",
            "blockers": ["comparison_input_invalid"],
            "error": str(exc),
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0 if payload["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
