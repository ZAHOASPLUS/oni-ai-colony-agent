"""Fail-closed paired-snapshot analysis for utility contents.

This module computes a mass change rate only when two complete, same-world
contents envelopes have matching instance/side identity and a positive time
interval. It never infers production, delivery, capacity, or causality.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping

from agent.state_quality import parse_snapshot_timestamp


def paired_contents_rates(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Return conservative gas/liquid mass deltas for matching endpoint cells."""
    old = previous.get("functional_building_utility_contents")
    new = current.get("functional_building_utility_contents")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return {"status": "unknown", "reason": "contents_field_missing", "samples": []}
    if old.get("schema") != new.get("schema") or old.get("schema") != "oni.functional_building_utility_contents.v1":
        return {"status": "unknown", "reason": "schema_mismatch", "samples": []}
    if old.get("complete") is not True or new.get("complete") is not True:
        return {"status": "unknown", "reason": "incomplete", "samples": []}
    if old.get("world_id") != new.get("world_id") or previous.get("world_id") != current.get("world_id"):
        return {"status": "unknown", "reason": "world_mismatch", "samples": []}
    t0 = parse_snapshot_timestamp(old.get("captured_at")); t1 = parse_snapshot_timestamp(new.get("captured_at"))
    if t0 is None or t1 is None:
        return {"status": "unknown", "reason": "timestamp_invalid", "samples": []}
    seconds = (t1 - t0).total_seconds()
    if seconds <= 0 or seconds > 3600:
        return {"status": "unknown", "reason": "interval_invalid", "samples": []}
    old_instances, new_instances = old.get("instances"), new.get("instances")
    if not isinstance(old_instances, list) or not isinstance(new_instances, list):
        return {"status": "unknown", "reason": "instances_invalid", "samples": []}
    def index_instances(items: list[Any]) -> dict[Any, Mapping[str, Any]] | None:
        indexed: dict[Any, Mapping[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                return None
            instance_id = item.get("instance_id")
            if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or instance_id in indexed:
                return None
            indexed[instance_id] = item
        return indexed
    old_by_id = index_instances(old_instances)
    new_by_id = index_instances(new_instances)
    if old_by_id is None or new_by_id is None:
        return {"status": "unknown", "reason": "instances_invalid", "samples": []}
    if set(old_by_id) != set(new_by_id) or not old_by_id:
        return {"status": "unknown", "reason": "instance_identity_mismatch", "samples": []}
    samples = []
    for iid in sorted(old_by_id):
        a, b = old_by_id[iid], new_by_id[iid]
        if a.get("world_id") != b.get("world_id") or a.get("prefab_id") != b.get("prefab_id"):
            return {"status": "unknown", "reason": "instance_identity_changed", "samples": []}
        for side_name in ("input", "output"):
            x, y = a.get(side_name), b.get(side_name)
            if not isinstance(x, Mapping) or not isinstance(y, Mapping):
                return {"status": "unknown", "reason": "side_missing", "samples": []}
            if x.get("declared") != y.get("declared") or x.get("cell") != y.get("cell") or x.get("content_kind") != y.get("content_kind"):
                return {"status": "unknown", "reason": "endpoint_identity_changed", "samples": []}
            kind = x.get("content_kind")
            if kind not in ("gas", "liquid"):
                continue
            if x.get("declared") is not True or y.get("declared") is not True:
                return {"status": "unknown", "reason": "endpoint_undeclared", "samples": []}
            if x.get("element") != y.get("element"):
                return {"status": "unknown", "reason": "element_changed", "samples": []}
            m0, m1 = x.get("mass"), y.get("mass")
            if not all(isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(float(m)) and m >= 0 for m in (m0, m1)):
                return {"status": "unknown", "reason": "mass_unknown", "samples": []}
            samples.append({"instance_id": iid, "prefab_id": a.get("prefab_id"), "side": side_name, "content_kind": kind, "element": x.get("element"), "interval_seconds": seconds, "mass_delta": float(m1) - float(m0), "mass_rate_per_second": (float(m1) - float(m0)) / seconds})
    return {"status": "observed", "interval_seconds": seconds, "samples": samples}


def sustained_contents_rates(
    snapshots: list[Mapping[str, Any]],
    *,
    min_snapshots: int = 3,
    relative_tolerance: float = 0.05,
    absolute_tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Summarize repeated gas/liquid mass rates without inferring causality.

    Every adjacent pair must pass :func:`paired_contents_rates`. A stream is
    marked stable only when each endpoint has at least two rates and its range
    is within the supplied absolute/relative tolerance of the largest rate
    magnitude. Any invalid input remains unknown.
    """
    if not isinstance(snapshots, list) or len(snapshots) < min_snapshots or min_snapshots < 3:
        return {"status": "unknown", "reason": "insufficient_snapshots", "streams": []}
    if (not isinstance(relative_tolerance, (int, float)) or isinstance(relative_tolerance, bool)
            or not math.isfinite(float(relative_tolerance)) or relative_tolerance < 0
            or not isinstance(absolute_tolerance, (int, float)) or isinstance(absolute_tolerance, bool)
            or not math.isfinite(float(absolute_tolerance)) or absolute_tolerance < 0):
        return {"status": "unknown", "reason": "tolerance_invalid", "streams": []}
    pairs = []
    for index, (previous, current) in enumerate(zip(snapshots, snapshots[1:])):
        if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
            return {"status": "unknown", "reason": "snapshot_invalid", "pair_index": index, "streams": []}
        result = paired_contents_rates(previous, current)
        if result.get("status") != "observed":
            return {"status": "unknown", "reason": result.get("reason", "pair_unknown"), "pair_index": index, "streams": []}
        pairs.append(result)
    by_key: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for pair in pairs:
        for sample in pair.get("samples", []):
            key = (sample.get("instance_id"), sample.get("side"))
            by_key.setdefault(key, []).append(sample)
    if not by_key or any(len(rates) != len(pairs) for rates in by_key.values()):
        return {"status": "unknown", "reason": "stream_identity_mismatch", "streams": []}
    streams = []
    for (instance_id, side), rates in sorted(by_key.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        values = [float(item["mass_rate_per_second"]) for item in rates]
        peak = max(abs(value) for value in values)
        allowed = float(absolute_tolerance) + float(relative_tolerance) * peak
        spread = max(values) - min(values)
        streams.append({
            "instance_id": instance_id,
            "prefab_id": rates[0].get("prefab_id"),
            "side": side,
            "content_kind": rates[0].get("content_kind"),
            "element": rates[0].get("element"),
            "interval_count": len(values),
            "rates_per_second": values,
            "average_rate_per_second": sum(values) / len(values),
            "min_rate_per_second": min(values),
            "max_rate_per_second": max(values),
            "spread_per_second": spread,
            "stable": spread <= allowed,
        })
    return {"status": "observed", "interval_count": len(pairs), "streams": streams}
