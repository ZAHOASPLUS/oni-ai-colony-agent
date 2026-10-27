using System;
using System.Collections.Generic;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Shortest four-neighbour cell path for a utility link (roadmap B15).
    ///
    /// Pure: cells are row-major ints (<c>y * width + x</c>) and the caller
    /// decides which cells a utility may occupy, so the search is testable
    /// without the game.  The search is bounded both by path length and by the
    /// number of cells visited, so a link that does not exist costs a fixed
    /// amount of main-thread time instead of a world-sized flood fill.
    /// </summary>
    internal static class UtilityPathPlanner
    {
        /// <summary>
        /// Returns the cells from <paramref name="start"/> to the nearest of
        /// <paramref name="targets"/>, both ends included, or null.  Neighbours
        /// are expanded in a fixed order (up, right, down, left), so equal-length
        /// paths always resolve the same way.
        /// </summary>
        internal static List<int> FindPath(
            int start,
            ICollection<int> targets,
            int width,
            int height,
            Func<int, bool> passable,
            int maxLength,
            int maxVisited)
        {
            if (targets == null || targets.Count == 0 || width <= 0 || height <= 0 ||
                maxLength < 1 || maxVisited < 1 || !InGrid(start, width, height) || !passable(start))
            {
                return null;
            }
            Dictionary<int, int> parent = new Dictionary<int, int> { { start, -1 } };
            Dictionary<int, int> depth = new Dictionary<int, int> { { start, 1 } };
            Queue<int> frontier = new Queue<int>();
            frontier.Enqueue(start);
            while (frontier.Count > 0)
            {
                int cell = frontier.Dequeue();
                if (targets.Contains(cell))
                {
                    return Unwind(parent, cell);
                }
                if (depth[cell] >= maxLength)
                {
                    continue;
                }
                foreach (int next in Neighbours(cell, width, height))
                {
                    if (parent.ContainsKey(next) || !passable(next))
                    {
                        continue;
                    }
                    if (parent.Count >= maxVisited)
                    {
                        return null;
                    }
                    parent[next] = cell;
                    depth[next] = depth[cell] + 1;
                    frontier.Enqueue(next);
                }
            }
            return null;
        }

        /// <summary>
        /// Visits <paramref name="stops"/> in order with one path, never reusing a
        /// cell; a utility that passes through a port cell connects that port.
        /// Returns null when any leg is missing or the total exceeds the limit.
        /// </summary>
        internal static List<int> FindTrunk(
            IList<int> stops,
            int width,
            int height,
            Func<int, bool> passable,
            int maxLength,
            int maxVisited)
        {
            if (stops == null || stops.Count < 2)
            {
                return null;
            }
            List<int> trunk = new List<int> { stops[0] };
            HashSet<int> used = new HashSet<int> { stops[0] };
            for (int index = 1; index < stops.Count; index++)
            {
                int from = trunk[trunk.Count - 1];
                int remaining = maxLength - trunk.Count + 1;
                List<int> leg = FindPath(
                    from,
                    new HashSet<int> { stops[index] },
                    width,
                    height,
                    cell => cell == from || (!used.Contains(cell) && passable(cell)),
                    remaining,
                    maxVisited);
                if (leg == null)
                {
                    return null;
                }
                for (int step = 1; step < leg.Count; step++)
                {
                    trunk.Add(leg[step]);
                    used.Add(leg[step]);
                }
            }
            return trunk.Count <= maxLength ? trunk : null;
        }

        private static bool InGrid(int cell, int width, int height)
        {
            return cell >= 0 && cell < width * height;
        }

        private static IEnumerable<int> Neighbours(int cell, int width, int height)
        {
            int x = cell % width;
            int y = cell / width;
            if (y + 1 < height)
            {
                yield return cell + width;
            }
            if (x + 1 < width)
            {
                yield return cell + 1;
            }
            if (y > 0)
            {
                yield return cell - width;
            }
            if (x > 0)
            {
                yield return cell - 1;
            }
        }

        private static List<int> Unwind(Dictionary<int, int> parent, int end)
        {
            List<int> path = new List<int>();
            for (int cell = end; cell != -1; cell = parent[cell])
            {
                path.Add(cell);
            }
            path.Reverse();
            return path;
        }
    }
}
