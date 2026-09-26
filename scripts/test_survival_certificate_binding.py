#!/usr/bin/env python3
"""Offline tests: a survival certificate cannot hide a blocking witness.

Review 2026-09-26 (S1): the frame certificate validator reduced the frame's
own witness list against the certificate's own active-alert set.  Editing a
higher-priority witness to resolved/pending, or deleting it, together with a
shortened active set, reduced consistently, so both the frame and the
approval validators accepted a selected lower-priority pause that the real
arbiter blocks.  No game, Bridge or live directory is involved.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import rules
from agent.approval_receipt import ApprovalReceiptError, validate_approval_inputs
from agent.canonical_hash import canonical_hash
import scripts.verify_approval_frame_binding as approval_binding
from scripts.verify_survival_certificate import make_state, projected_incident, source_for, witness


def forged_low_oxygen_certificate() -> dict:
    return {
        "schema": rules._SURVIVAL_CERTIFICATE_SCHEMA,
        "policy_hash": rules.survival_alert_policy_hash(),
        "status": "selected",
        "blockers": [],
        "game_paused": False,
        "active_pause_alert_ids": ["low_oxygen"],
        "eligible_incident_ids": ["low_oxygen"],
        "selected_incident_id": "low_oxygen",
        "command_hint_hash": canonical_hash(rules._alert_signal("low_oxygen", True)["command"]),
    }


class CertificateBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = make_state(["low_oxygen", "suffocation"])
        # The real suffocation episode cannot authorize (risk mismatch), so the
        # real arbiter blocks instead of falling through to low oxygen.
        self.bad_high = witness(self.state, "suffocation", risk="medium")
        self.low = witness(self.state, "low_oxygen")
        real = rules.arbitrate_survival_emergency(self.state, [self.bad_high, self.low])
        self.assertEqual(real["status"], "blocked")
        self.source = source_for(self.state)

    def blockers(self, items: list, *, state=None) -> list:
        return rules.survival_certificate_blockers(
            forged_low_oxygen_certificate(),
            self.source,
            [projected_incident(item) for item in items],
            state=state,
        )

    def test_edited_higher_witness_status_is_rejected(self) -> None:
        for status in ("resolved", "pending", "cooldown", "inactive"):
            with self.subTest(status=status):
                edited = dict(self.bad_high, status=status)
                self.assertIn(
                    "survival_certificate_active_alert_unbound:suffocation",
                    self.blockers([edited, self.low]),
                )

    def test_deleted_witness_is_caught_by_rearbitration(self) -> None:
        # The frame alone no longer shows suffocation; only the frame's own
        # snapshot can.
        self.assertEqual(self.blockers([self.low]), [])
        self.assertEqual(
            self.blockers([self.low], state=self.state),
            ["survival_certificate_rearbitration_mismatch"],
        )

    def test_state_must_be_the_frame_snapshot(self) -> None:
        other = copy.deepcopy(self.state)
        other["alerts"] = ["low_oxygen"]
        self.assertIn("survival_certificate_state_unbound", self.blockers([self.low], state=other))

    def test_genuine_certificate_passes_with_its_state(self) -> None:
        state = make_state(["suffocation"])
        items = [
            witness(state, "suffocation"),
            witness(state, "low_oxygen", status="inactive", telemetry_state="inactive"),
        ]
        certificate = rules.build_survival_arbitration_certificate(state, items)
        self.assertEqual(certificate["status"], "selected")
        projected = [projected_incident(item) for item in items]
        self.assertEqual(rules.survival_certificate_blockers(certificate, source_for(state), projected), [])
        self.assertEqual(
            rules.survival_certificate_blockers(certificate, source_for(state), projected, state=state), []
        )


class ApprovalRearbitrationTests(unittest.TestCase):
    def fixture(self) -> tuple:
        captured = {}
        real_build = approval_binding.build

        def capture(values):
            captured["state"] = copy.deepcopy(values["state"])
            return real_build(values)

        with mock.patch.object(approval_binding, "build", capture):
            command, policy, frame = approval_binding.fixture()
        return command, policy, frame, captured["state"]

    def test_suggested_pause_is_rearbitrated_against_its_state(self) -> None:
        command, policy, frame, state = self.fixture()
        validate_approval_inputs(command, policy, frame)
        validate_approval_inputs(command, policy, frame, survival_state=state)
        moved_on = copy.deepcopy(state)
        moved_on["alerts"] = []
        with self.assertRaises(ApprovalReceiptError) as raised:
            validate_approval_inputs(command, policy, frame, survival_state=moved_on)
        self.assertIn("survival_certificate_state_unbound", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
