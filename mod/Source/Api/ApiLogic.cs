using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Automation ports, rooms and activation ranges (Smart Battery and the
    /// like), read and set the way the game's own side screens do.
    /// </summary>
    internal static class ApiLogic
    {
        // Offsets from the building's origin cell, unrotated, as for power_port.
        internal static List<object> DescribeDefPorts(BuildingDef def)
        {
            List<object> ports = new List<object>();
            AddDefPorts(ports, def.LogicInputPorts, "input");
            AddDefPorts(ports, def.LogicOutputPorts, "output");
            return ports;
        }

        private static void AddDefPorts(List<object> ports, List<LogicPorts.Port> source, string kind)
        {
            if (source == null)
            {
                return;
            }
            foreach (LogicPorts.Port port in source)
            {
                ports.Add(new Dictionary<string, object>
                {
                    { "kind", kind },
                    { "offset", new[] { port.cellOffset.x, port.cellOffset.y } },
                    { "description", Strip(port.description) }
                });
            }
        }

        // The cells a placed building's ports sit on, rotation included, with
        // whether a wire reaches each and the signal now on it (1 green, 0 red).
        internal static List<object> DescribePlacedPorts(GameObject go)
        {
            List<object> ports = new List<object>();
            LogicPorts logic = go.GetComponent<LogicPorts>();
            if (logic == null)
            {
                return ports;
            }
            AddPlacedPorts(ports, logic, logic.inputPortInfo, "input");
            AddPlacedPorts(ports, logic, logic.outputPortInfo, "output");
            return ports;
        }

        private static void AddPlacedPorts(List<object> ports, LogicPorts logic, LogicPorts.Port[] source, string kind)
        {
            if (source == null)
            {
                return;
            }
            // LogicPorts.GetActualCell is private; this is the same sum.
            Rotatable rotatable = logic.GetComponent<Rotatable>();
            int origin = Grid.PosToCell(logic.gameObject);
            foreach (LogicPorts.Port port in source)
            {
                CellOffset offset = rotatable == null ? port.cellOffset : rotatable.GetRotatedCellOffset(port.cellOffset);
                Dictionary<string, object> entry = ApiValues.Xy(Grid.OffsetCell(origin, offset));
                entry["kind"] = kind;
                entry["description"] = Strip(port.description);
                entry["connected"] = logic.IsPortConnected(port.id);
                entry["signal"] = kind == "input" ? logic.GetInputValue(port.id) : logic.GetOutputValue(port.id);
                ports.Add(entry);
            }
        }

        /// <summary>The room the game sees at this cell, or null outside any room.</summary>
        internal static object DescribeRoom(int cell)
        {
            CavityInfo cavity = Game.Instance.roomProber.GetCavityForCell(cell);
            if (cavity == null)
            {
                return null;
            }
            Room room = cavity.room;
            RoomType type = room?.roomType;
            return new Dictionary<string, object>
            {
                { "type", type == null ? null : type.Id },
                { "name", type == null ? null : Strip(type.Name) },
                { "cells", cavity.NumCells },
                { "min", new[] { cavity.minX, cavity.minY } },
                { "max", new[] { cavity.maxX, cavity.maxY } }
            };
        }

        internal static void DescribeBattery(GameObject go, Dictionary<string, object> entry)
        {
            BatterySmart battery = go.GetComponent<BatterySmart>();
            if (battery == null)
            {
                return;
            }
            entry["smart_battery_low_pct"] = Low(battery);
            entry["smart_battery_high_pct"] = High(battery);
            entry["charge_pct"] = battery.Capacity > 0f
                ? ApiValues.Number(100f * battery.JoulesAvailable / battery.Capacity, 0)
                : (object)null;
        }

        /// <summary>
        /// smart_battery_low_pct: the output turns green when the charge falls to
        /// it.  smart_battery_high_pct: the output turns red when the charge rises
        /// to it.  The range is checked before anything changes.
        /// </summary>
        internal static bool Configure(GameObject building, JObject args, Dictionary<string, object> applied)
        {
            bool setLow = args.Property("smart_battery_low_pct") != null;
            bool setHigh = args.Property("smart_battery_high_pct") != null;
            if (!setLow && !setHigh)
            {
                return false;
            }
            BatterySmart battery = building.GetComponent<BatterySmart>();
            if (battery == null || building.GetComponent<BuildingComplete>() == null)
            {
                throw new ApiException("unsupported_setting", "Battery thresholds apply only to a finished BatterySmart.");
            }
            float low = ReadPercent(args, "smart_battery_low_pct", Low(battery));
            float high = ReadPercent(args, "smart_battery_high_pct", High(battery));
            if (low > high)
            {
                throw new ApiException(
                    "invalid_argument",
                    "smart_battery_low_pct must not exceed smart_battery_high_pct; supply both to change the range.");
            }
            // ActiveRangeSideScreen writes these same properties; each setter
            // also re-evaluates the output signal.
            battery.DeactivateValue = low;
            battery.ActivateValue = high;
            applied["smart_battery_low_pct"] = Low(battery);
            applied["smart_battery_high_pct"] = High(battery);
            return true;
        }

        // BatterySmart's properties cross over its fields: ActivateValue reads
        // and writes the field deactivateValue and DeactivateValue the field
        // activateValue (checked in the game's IL, 2026-11-06).
        // UpdateLogicCircuit turns the output on at charge <= activateValue
        // and off at charge >= deactivateValue, so the low threshold is the
        // DeactivateValue property and the high one the ActivateValue property.
        private static float Low(BatterySmart battery)
        {
            return battery.DeactivateValue;
        }

        private static float High(BatterySmart battery)
        {
            return battery.ActivateValue;
        }

        private static float ReadPercent(JObject args, string name, float fallback)
        {
            JToken value = args[name];
            if (value == null)
            {
                return fallback;
            }
            if (value.Type != JTokenType.Integer || value.Value<double>() < 0 || value.Value<double>() > 100)
            {
                throw new ApiException("invalid_argument", name + " must be an integer from 0 to 100.");
            }
            return value.Value<int>();
        }

        private static string Strip(string text)
        {
            return string.IsNullOrEmpty(text) ? text : STRINGS.UI.StripLinkFormatting(text);
        }
    }
}
