using System;
using System.Collections.Generic;
using System.Linq;

namespace OniAiProbeMod.Probe
{
    // Checks the pure supply-link path search (roadmap B15) without the game:
    // the Probe passes the game's own placement check as the passable callback.
    internal static class Program
    {
        private static int failures;

        private static int Main()
        {
            // 6 x 4 grid, cells are y * 6 + x.
            const int width = 6;
            const int height = 4;
            Func<int, bool> open = cell => true;

            Check("straight_line", UtilityPathPlanner.FindPath(0, new[] { 3 }, width, height, open, 64, 1000),
                new[] { 0, 1, 2, 3 });

            // A wall at x = 2 for y = 0..2 forces the path over the top row.
            HashSet<int> wall = new HashSet<int> { 2, 8, 14 };
            Check("detour_around_wall",
                UtilityPathPlanner.FindPath(0, new[] { 4 }, width, height, cell => !wall.Contains(cell), 64, 1000),
                new[] { 0, 6, 12, 18, 19, 20, 21, 15, 9, 3, 4 }, exactCells: false, length: 11);

            HashSet<int> sealedOff = new HashSet<int> { 2, 8, 14, 20 };
            Check("no_route", UtilityPathPlanner.FindPath(0, new[] { 4 }, width, height,
                cell => !sealedOff.Contains(cell), 64, 1000), null);

            Check("too_long", UtilityPathPlanner.FindPath(0, new[] { 5 }, width, height, open, 5, 1000), null);
            Check("exact_length_limit", UtilityPathPlanner.FindPath(0, new[] { 5 }, width, height, open, 6, 1000),
                new[] { 0, 1, 2, 3, 4, 5 });
            Check("visit_bound", UtilityPathPlanner.FindPath(0, new[] { 23 }, width, height, open, 64, 4), null);

            // x = 5 on row 0 and x = 0 on row 1 are consecutive ints but not neighbours.
            HashSet<int> edge = new HashSet<int> { 5, 6 };
            Check("no_row_wrap", UtilityPathPlanner.FindPath(5, new[] { 6 }, width, height,
                cell => edge.Contains(cell), 64, 1000), null);

            Check("blocked_start", UtilityPathPlanner.FindPath(0, new[] { 3 }, width, height,
                cell => cell != 0, 64, 1000), null);
            Check("start_is_target", UtilityPathPlanner.FindPath(7, new[] { 7 }, width, height, open, 64, 1000),
                new[] { 7 });
            Check("nearest_of_two_targets", UtilityPathPlanner.FindPath(0, new[] { 5, 2 }, width, height, open, 64, 1000),
                new[] { 0, 1, 2 });
            // Up is expanded before right, so an L-shaped tie goes up first.
            Check("deterministic_tie", UtilityPathPlanner.FindPath(0, new[] { 7 }, width, height, open, 64, 1000),
                new[] { 0, 6, 7 });

            // A trunk visits its stops in order and never reuses a cell.
            List<int> trunk = UtilityPathPlanner.FindTrunk(new[] { 0, 3, 21 }, width, height, open, 64, 1000);
            Check("trunk_visits_stops", trunk, null, exactCells: false, length: trunk?.Count ?? -1, allowAny: true);
            Expect("trunk_order", trunk != null && trunk[0] == 0 && trunk.IndexOf(3) > 0 && trunk.Last() == 21);
            Expect("trunk_no_reuse", trunk != null && trunk.Distinct().Count() == trunk.Count);
            Expect("trunk_adjacent", trunk != null && Adjacent(trunk, width));
            Check("trunk_too_long", UtilityPathPlanner.FindTrunk(new[] { 0, 5, 23 }, width, height, open, 8, 1000), null);
            Check("trunk_missing_leg", UtilityPathPlanner.FindTrunk(new[] { 0, 4 }, width, height,
                cell => !sealedOff.Contains(cell), 64, 1000), null);

            Console.WriteLine(failures == 0 ? "all_checks_passed" : $"failures={failures}");
            return failures == 0 ? 0 : 1;
        }

        private static bool Adjacent(IList<int> cells, int width)
        {
            for (int index = 1; index < cells.Count; index++)
            {
                int ax = cells[index - 1] % width, ay = cells[index - 1] / width;
                int bx = cells[index] % width, by = cells[index] / width;
                if (Math.Abs(ax - bx) + Math.Abs(ay - by) != 1)
                {
                    return false;
                }
            }
            return true;
        }

        private static void Check(string name, List<int> actual, int[] expected,
            bool exactCells = true, int length = -1, bool allowAny = false)
        {
            bool ok;
            if (allowAny)
            {
                ok = actual != null;
            }
            else if (expected == null)
            {
                ok = actual == null;
            }
            else if (exactCells)
            {
                ok = actual != null && actual.SequenceEqual(expected);
            }
            else
            {
                ok = actual != null && actual.Count == length && actual[0] == expected[0] &&
                    actual.Last() == expected.Last() && Adjacent(actual, 6);
            }
            Expect(name, ok, actual == null ? "null" : string.Join(",", actual));
        }

        private static void Expect(string name, bool ok, string detail = "")
        {
            if (!ok)
            {
                failures++;
            }
            Console.WriteLine((ok ? "ok " : "FAIL ") + name + (ok || detail.Length == 0 ? "" : " got " + detail));
        }
    }
}
