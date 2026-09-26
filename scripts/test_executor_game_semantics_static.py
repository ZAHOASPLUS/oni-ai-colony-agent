"""Static guards for executor game-API fixes (code review 2026-09-26, G1/L11).

These run without the game, so they only pin the source shape against
regression; the live effect of each fix still requires a clone run.

G1: a Wire path must register its own adjacency (AddConnection, both
directions) before each blueprint reads its connections, as ONI's
WireBuildTool does; otherwise the built path is isolated single cells.

L11: set_research may only target an unfinished tech whose prerequisites
are complete; otherwise SetActiveResearch activates a prerequisite and the
research.current postcondition can never hold.
"""

from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs"


class ExecutorGameSemanticsStaticTests(unittest.TestCase):
    source = EXECUTOR.read_text(encoding="utf-8")

    def test_wire_path_registers_adjacency_before_reading_connections(self):
        block = self.source[self.source.index("if (isUtilityPath)\n            {\n                // Register"):]
        block = block[: block.index("ProbeStateReader.RegisterUtilityPath")]
        first_add = block.index("utilityManager.AddConnection(direction,")
        inverse_add = block.index("utilityManager.AddConnection(direction.InverseDirection(),")
        read = block.index("utilityManager.GetConnections(")
        self.assertLess(first_add, read)
        self.assertLess(inverse_add, read)
        self.assertIn("UtilityConnectionsExtensions.DirectionFromToCell(", block)

    def test_set_research_requires_an_available_target(self):
        start = self.source.index('case "set_research":\n                {')
        case = self.source[start: self.source.index('case "set_duplicant_chore_priority":', start)]
        self.assertIn("techDefinition.IsComplete()", case)
        self.assertIn("techDefinition.ArePrerequisitesComplete()", case)


if __name__ == "__main__":
    unittest.main()
