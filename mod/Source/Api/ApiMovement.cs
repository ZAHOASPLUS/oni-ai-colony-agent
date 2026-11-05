using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The game's own "Move To" order, as MoveToLocationTool.SetMoveToLocation
    /// gives it: the duplicant walks to the cell and waits there until it gets
    /// new work.  Refused when the game's navigator cannot reach the cell.
    /// </summary>
    internal static class ApiMovement
    {
        internal static object MoveTo(JObject args)
        {
            MinionIdentity identity = ApiDuplicants.Read(args, "duplicant");
            int x = ApiArgs.Int(args, "x", int.MinValue);
            int y = ApiArgs.Int(args, "y", int.MinValue);
            if (x == int.MinValue || y == int.MinValue)
            {
                throw new ApiException("invalid_argument", "x and y are required.");
            }
            int cell = ApiValues.CheckedCell(x, y);
            int from = Grid.PosToCell(identity.gameObject);

            KPrefabID prefab = identity.GetComponent<KPrefabID>();
            if (prefab != null && prefab.HasTag(GameTags.Incapacitated))
            {
                throw new ApiException(
                    "cannot_move",
                    identity.GetProperName() + " is incapacitated and cannot walk.",
                    "Another duplicant carries an incapacitated duplicant to a free Triage Cot by itself; keep one built and reachable.");
            }
            Navigator navigator = identity.GetComponent<Navigator>();
            MoveToLocationMonitor.Instance monitor = navigator == null ? null : navigator.GetSMI<MoveToLocationMonitor.Instance>();
            if (monitor == null)
            {
                throw new ApiException("cannot_move", identity.GetProperName() + " cannot take a move order.");
            }
            if (!navigator.CanReach(cell))
            {
                throw new ApiException(
                    "unreachable",
                    identity.GetProperName() + " cannot reach (" + x + ", " + y + ") from (" +
                    Grid.CellToXY(from).x + ", " + Grid.CellToXY(from).y + ").",
                    "Pick a cell the duplicant can stand in and walk to: floor below it, no solid tile, ladders where it must climb.");
            }

            monitor.MoveToLocation(cell);
            Dictionary<string, object> result = new Dictionary<string, object>
            {
                { "duplicant", ApiDuplicants.Id(identity) },
                { "name", identity.GetProperName() },
                { "from", ApiValues.Xy(from) },
                { "to", ApiValues.Xy(cell) },
                { "navigation_cost", navigator.GetNavigationCost(cell) }
            };
            return result;
        }
    }
}
