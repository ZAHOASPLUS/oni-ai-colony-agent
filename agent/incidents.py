from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.state_quality import parse_timestamp


def _now_for_snapshot(snapshot: Dict[str, Any]) -> dt.datetime:
    parsed = parse_timestamp(snapshot.get("ts"))
    return parsed or dt.datetime.now(dt.timezone.utc).astimezone()


def _iso(value: dt.datetime) -> str:
    # Preserve sub-second snapshot identity.  Probe timestamps may contain
    # seven fractional digits; Python normalizes those to microseconds, which
    # remains time-equal without collapsing a fresh sample to the prior second.
    return value.isoformat(timespec="microseconds")


class IncidentValidationError(ValueError):
    """Raised when an incident signal violates the tracker input contract."""


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _snapshot_follows(previous: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    """Return whether ``snapshot`` is the next sample after the episode's last.

    Survival witnesses must prove every sample from first to last
    (``last_snapshot_seq - first_snapshot_seq + 1 == consecutive_samples``).
    Inputs without snapshot sequences keep the historical count; the arbiter
    rejects such witnesses on sequence grounds anyway.
    """

    last = previous.get("last_snapshot_seq")
    current = snapshot.get("snapshot_seq")
    if not _plain_int(last) or not _plain_int(current):
        return True
    return current == last + 1


def _validate_non_negative_number(
    signal: Mapping[str, Any], key: str, *, integer: bool = False
) -> int | float:
    """Read one numeric policy value without coercing malformed input.

    Missing fields retain the historical defaults.  A present value must be
    non-boolean, finite, convertible to the requested numeric type, and
    non-negative.  ``required_consecutive_samples`` keeps the historical
    ``max(1, ...)`` treatment for zero after validation.
    """

    if key not in signal:
        return 1 if integer else 0.0

    value = signal[key]
    if isinstance(value, bool):
        raise IncidentValidationError(f"signal.{key} must not be a bool")

    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IncidentValidationError(f"signal.{key} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise IncidentValidationError(f"signal.{key} must be finite")
    if numeric < 0:
        raise IncidentValidationError(f"signal.{key} must be non-negative")

    if integer:
        try:
            converted = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise IncidentValidationError(
                f"signal.{key} must be convertible to an integer"
            ) from exc
        if converted != numeric:
            raise IncidentValidationError(
                f"signal.{key} must be an integer-valued finite number"
            )
        if converted < 0:
            raise IncidentValidationError(f"signal.{key} must be non-negative")
        return max(1, converted)
    return numeric


def _validate_signal(signal: Any) -> Tuple[Mapping[str, Any], str, str, int, float, float]:
    """Validate the fields consumed by :class:`IncidentTracker`.

    Validation is deliberately side-effect free so callers can preflight a
    complete signal batch before changing tracker state.
    """

    if not isinstance(signal, Mapping):
        raise IncidentValidationError("signal must be a mapping")

    incident_id = signal.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise IncidentValidationError("signal.incident_id must be a non-empty string")

    state = str(signal.get("state") or "unknown")
    required_samples = _validate_non_negative_number(
        signal, "required_consecutive_samples", integer=True
    )
    min_duration = _validate_non_negative_number(signal, "min_duration_seconds")
    cooldown_seconds = _validate_non_negative_number(signal, "cooldown_seconds")
    return signal, incident_id, state, required_samples, min_duration, cooldown_seconds


class IncidentTracker:
    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self.timeline: List[Dict[str, Any]] = []

    def reset(self, session_id: Optional[str]) -> None:
        self.session_id = session_id
        self.incidents = {}
        self.timeline = []

    def update(self, snapshot: Dict[str, Any], signals: Iterable[Mapping[str, Any]]) -> None:
        if isinstance(signals, Mapping) or isinstance(signals, (str, bytes, bytearray)):
            raise IncidentValidationError("signals must be an iterable of mappings")
        try:
            signal_batch = list(signals)
        except TypeError as exc:
            raise IncidentValidationError("signals must be an iterable of mappings") from exc

        # Preflight every item before resetting the session or applying the
        # first update.  A malformed later item therefore cannot leave a
        # partially updated incidents/timeline pair behind.
        for signal in signal_batch:
            _validate_signal(signal)

        session_id = snapshot.get("session_id")
        if self.session_id != session_id:
            self.reset(session_id)

        now = _now_for_snapshot(snapshot)
        for signal in signal_batch:
            self._update_one(snapshot, signal, now)

    def _update_one(self, snapshot: Dict[str, Any], signal: Mapping[str, Any], now: dt.datetime) -> None:
        _, incident_id, state, required_samples, min_duration, cooldown_seconds = _validate_signal(signal)
        previous = self.incidents.get(incident_id)

        if previous is None:
            previous = {
                "schema": "oni.incident.v1",
                "incident_id": incident_id,
                "status": "inactive",
                "consecutive_samples": 0,
                "first_seen": None,
                "first_seq": None,
                "first_snapshot_seq": None,
                "last_seen": None,
                "last_snapshot_seq": None,
                "resolved_at": None,
                "cooldown_until": None,
            }

        incident = dict(previous)
        incident.update(
            {
                "title": signal.get("title"),
                "risk": signal.get("risk"),
                "reason": signal.get("reason"),
                "action": signal.get("action"),
                "evidence": signal.get("evidence"),
                "command": signal.get("command"),
                "telemetry_state": state,
                "session_id": snapshot.get("session_id"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "last_seq": snapshot.get("seq"),
                "last_snapshot_seq": snapshot.get("snapshot_seq"),
            }
        )

        old_status = str(previous.get("status") or "inactive")
        if state == "unknown":
            incident["status"] = "blocked_unknown"
            incident["consecutive_samples"] = 0
            incident["last_seen"] = _iso(now)
            self._transition(old_status, "blocked_unknown", incident, now)
            self.incidents[incident_id] = incident
            return

        if state == "inactive":
            incident["consecutive_samples"] = 0
            incident["last_seen"] = _iso(now)
            if old_status in {"open", "pending", "blocked_unknown", "cooldown"}:
                incident["status"] = "resolved"
                incident["resolved_at"] = _iso(now)
                incident["cooldown_until"] = _iso(now + dt.timedelta(seconds=cooldown_seconds))
                self._append_event("incident_resolved", incident, now)
            else:
                incident["status"] = "inactive"
            self.incidents[incident_id] = incident
            return

        continuing = old_status in {"pending", "open", "cooldown"}
        if continuing and not _snapshot_follows(previous, snapshot):
            # A sample was skipped (the Bridge does not update incidents for
            # untrusted snapshots) or repeated, so this episode can no longer
            # prove continuity.  Restart it at this sample instead of carrying
            # an unprovable count that blocks every survival arbitration until
            # the alert clears (code review 2026-09-26, L8).
            continuing = False
        if not continuing:
            incident["first_seen"] = _iso(now)
            incident["first_seq"] = snapshot.get("seq")
            incident["first_snapshot_seq"] = snapshot.get("snapshot_seq")
            incident["consecutive_samples"] = 1
            incident["resolved_at"] = None
        else:
            incident["consecutive_samples"] = int(previous.get("consecutive_samples") or 0) + 1
        incident["last_seen"] = _iso(now)

        first_seen = parse_timestamp(incident.get("first_seen")) or now
        elapsed = max(0.0, (now - first_seen).total_seconds())
        cooldown_until = parse_timestamp(previous.get("cooldown_until"))
        in_cooldown = cooldown_until is not None and now < cooldown_until
        ready = incident["consecutive_samples"] >= required_samples and elapsed >= min_duration

        if in_cooldown:
            incident["status"] = "cooldown"
        elif ready:
            incident["status"] = "open"
            if old_status != "open":
                self._append_event("incident_opened", incident, now)
            else:
                self._append_event("incident_updated", incident, now)
        else:
            incident["status"] = "pending"

        self.incidents[incident_id] = incident

    def _transition(self, old_status: str, new_status: str, incident: Dict[str, Any], now: dt.datetime) -> None:
        if old_status != new_status:
            self._append_event("incident_" + new_status, incident, now)

    def _append_event(self, event: str, incident: Dict[str, Any], now: dt.datetime) -> None:
        self.timeline.append(
            {
                "schema": "oni.incident.event.v1",
                "event": event,
                "incident_id": incident.get("incident_id"),
                "status": incident.get("status"),
                "session_id": incident.get("session_id"),
                "snapshot_id": incident.get("snapshot_id"),
                "ts": _iso(now),
            }
        )
        self.timeline = self.timeline[-100:]

    def open_incidents(self) -> List[Dict[str, Any]]:
        risk_order = {"high": 0, "medium": 1, "low": 2}
        values = [item for item in self.incidents.values() if item.get("status") == "open"]
        return sorted(values, key=lambda item: (risk_order.get(str(item.get("risk")), 99), str(item.get("incident_id"))))

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": "oni.incidents.v1",
            "session_id": self.session_id,
            "items": sorted(self.incidents.values(), key=lambda item: str(item.get("incident_id"))),
            "timeline": list(self.timeline),
        }
