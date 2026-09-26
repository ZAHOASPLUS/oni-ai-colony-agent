"""Regression: the runtime configurator must disable mods the way ONI loads them (review 2026-09-26, S2).

ONI loads a mod when ``enabledForDlc`` contains the active version
(``KMod.Mod.IsEnabledForActiveDlc`` with ``DlcManager.GetHighestActiveDlcId``,
whose ``RELEASE_ORDER`` is ``["", "EXPANSION1_ID"]``).  The configurator only
rewrote the legacy ``enabled`` flag, so under Spaced Out every other mod stayed
loaded and the Probe was loaded only if the user had enabled it before.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import configure_live_validation_runtime as core  # noqa: E402

GAME_VERSIONS = ("", "EXPANSION1_ID")


def mod_entry(static_id: str, enabled: bool, dlc_text: str) -> str:
    return (
        "    {\r\n"
        '      "label": {\r\n'
        '        "distribution_platform": 1,\r\n'
        f'        "id": "{static_id}",\r\n'
        f'        "title": "{static_id}"\r\n'
        "      },\r\n"
        '      "status": 1,\r\n'
        f'      "enabled": {"true" if enabled else "false"},\r\n'
        f'      "enabledForDlc": {dlc_text},\r\n'
        '      "crash_count": 0,\r\n'
        f'      "staticID": "{static_id}"\r\n'
        "    }"
    )


def mods_payload(entries: list[str]) -> bytes:
    body = ",\r\n".join(entries)
    return ('{\r\n  "version": 1,\r\n  "mods": [\r\n' + body + '\r\n  ]\r\n}').encode("utf-8")


def loaded(document: dict, version: str) -> list[str]:
    # Mirror of KMod.Mod.IsEnabledForDlc(GetHighestActiveDlcId()).
    return [item["staticID"] for item in document["mods"] if version in item["enabledForDlc"]]


class ModsJsonDlcEnablementTests(unittest.TestCase):
    def production_shaped(self) -> bytes:
        return mods_payload([
            mod_entry("fixture.spaced_out_only", False, '[\r\n        "EXPANSION1_ID"\r\n      ]'),
            mod_entry("fixture.both_versions", True, '[\r\n        "",\r\n        "EXPANSION1_ID"\r\n      ]'),
            mod_entry("fixture.disabled", False, "[]"),
            mod_entry(core.PROBE_STATIC_ID, False, '[\r\n        "EXPANSION1_ID"\r\n      ]'),
        ])

    def test_only_the_probe_loads_in_both_game_versions(self):
        payload, count = core.build_mods_json(self.production_shaped())
        self.assertEqual(count, 4)
        document = json.loads(payload.decode("utf-8"))
        for version in GAME_VERSIONS:
            self.assertEqual(loaded(document, version), [core.PROBE_STATIC_ID], repr(version))
        self.assertEqual([item["staticID"] for item in document["mods"] if item["enabled"]],
                         [core.PROBE_STATIC_ID])

    def test_only_enablement_values_change(self):
        source = self.production_shaped()
        payload, _ = core.build_mods_json(source)
        self.assertNotIn(b"\n", payload.replace(b"\r\n", b""))  # CRLF kept, no bare LF
        before = json.loads(source.decode("utf-8"))["mods"]
        after = json.loads(payload.decode("utf-8"))["mods"]
        for old, new in zip(before, after):
            projected = dict(old, enabled=new["enabled"], enabledForDlc=new["enabledForDlc"])
            self.assertEqual(projected, new)
        self.assertEqual(core.build_mods_json(payload)[0], payload)  # idempotent

    def test_entry_without_enabled_for_dlc_is_refused(self):
        entry = mod_entry("fixture.legacy", True, "[]").replace('      "enabledForDlc": [],\r\n', "")
        payload = mods_payload([entry, mod_entry(core.PROBE_STATIC_ID, False, "[]")])
        with self.assertRaises(core.ConfigError):
            core.build_mods_json(payload)

    def test_non_string_enabled_for_dlc_is_refused(self):
        payload = mods_payload([
            mod_entry("fixture.odd", False, "[1]"),
            mod_entry(core.PROBE_STATIC_ID, False, "[]"),
        ])
        with self.assertRaises(core.ConfigError):
            core.build_mods_json(payload)


if __name__ == "__main__":
    unittest.main()
