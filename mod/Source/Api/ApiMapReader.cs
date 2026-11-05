using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Renders an area as text, one character per cell, one grid per layer.  An
    /// area above the size limit is downsampled so a whole asteroid fits.
    /// </summary>
    internal static class ApiMapReader
    {
        private const int MaxWidth = 64;
        private const int MaxHeight = 48;
        private const char Unrevealed = '?';
        private const char Vacuum = ' ';
        private const char Solid = '#';
        private const char AnyLiquid = '~';
        private const char AnyGas = '.';
        private const char Duplicant = '@';
        private const char Several = '+';

        // Bands in Celsius, coldest first; the comfortable band is 'e'.
        private static readonly float[] TemperatureLimits = { -20f, 0f, 10f, 20f, 30f, 40f, 60f, 100f, 300f };
        private const string TemperatureChars = "abcdefghij";
        private static readonly string[] TemperatureMeanings =
        {
            "below -20C", "-20..0C", "0..10C", "10..20C", "20..30C",
            "30..40C", "40..60C", "60..100C", "100..300C", "above 300C"
        };

        private static readonly KeyValuePair<ObjectLayer, char>[] UtilityLayers =
        {
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.Wire, 'w'),
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.LiquidConduit, 'l'),
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.GasConduit, 'g'),
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.SolidConduit, 's'),
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.LogicWire, 'a'),
            new KeyValuePair<ObjectLayer, char>(ObjectLayer.TravelTube, 't')
        };

        private static readonly ObjectLayer[] BuildingLayers =
        {
            ObjectLayer.Building,
            ObjectLayer.FoundationTile,
            ObjectLayer.LadderTile,
            ObjectLayer.Gantry
        };

        // Characters stay the same for a whole session, so the map a player saw
        // earlier can still be read after asking for another area.
        private static readonly Palette SolidPalette = new Palette(
            new KeyValuePair<string, char>("SandStone", 'S'),
            new KeyValuePair<string, char>("Sand", 's'),
            new KeyValuePair<string, char>("Algae", 'A'),
            new KeyValuePair<string, char>("Dirt", 'D'),
            new KeyValuePair<string, char>("Granite", 'G'),
            new KeyValuePair<string, char>("IgneousRock", 'I'),
            new KeyValuePair<string, char>("Cuprite", 'C'),
            new KeyValuePair<string, char>("Carbon", 'F'));
        private static readonly Palette LiquidPalette = new Palette(
            new KeyValuePair<string, char>("Water", 'W'),
            new KeyValuePair<string, char>("DirtyWater", 'D'),
            new KeyValuePair<string, char>("SaltWater", 'S'),
            new KeyValuePair<string, char>("Brine", 'B'));
        private static readonly Palette GasPalette = new Palette(
            new KeyValuePair<string, char>("Oxygen", 'O'),
            new KeyValuePair<string, char>("ContaminatedOxygen", 'P'),
            new KeyValuePair<string, char>("CarbonDioxide", 'C'),
            new KeyValuePair<string, char>("Hydrogen", 'H'),
            new KeyValuePair<string, char>("Methane", 'M'));
        private static readonly Palette BuildingPalette = new Palette();

        private sealed class Palette
        {
            private const string Pool = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$%&*=^";
            private readonly Dictionary<string, char> byName = new Dictionary<string, char>(StringComparer.Ordinal);
            private readonly HashSet<char> taken = new HashSet<char>();

            internal Palette(params KeyValuePair<string, char>[] preset)
            {
                foreach (KeyValuePair<string, char> entry in preset)
                {
                    byName[entry.Key] = entry.Value;
                    taken.Add(entry.Value);
                }
            }

            internal char For(string name)
            {
                char symbol;
                if (byName.TryGetValue(name, out symbol))
                {
                    return symbol;
                }
                char first = char.ToUpperInvariant(name[0]);
                char second = char.ToLowerInvariant(name[0]);
                if (Pool.IndexOf(first) >= 0 && !taken.Contains(first))
                {
                    symbol = first;
                }
                else if (Pool.IndexOf(second) >= 0 && !taken.Contains(second))
                {
                    symbol = second;
                }
                else
                {
                    symbol = Pool.FirstOrDefault(candidate => !taken.Contains(candidate));
                    if (symbol == default(char))
                    {
                        // The pool has run out: stay readable and let inspect tell them apart.
                        return Several;
                    }
                }
                taken.Add(symbol);
                byName[name] = symbol;
                return symbol;
            }
        }

        // A layer renders a cell to a character and says what the character means.
        private delegate char CellRenderer(int cell, Dictionary<char, string> legend);

        internal static object Read(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            int worldXMin = (int)world.minimumBounds.x;
            int worldYMin = (int)world.minimumBounds.y;
            int worldXMax = (int)world.maximumBounds.x;
            int worldYMax = (int)world.maximumBounds.y;

            int xMin = Math.Max(worldXMin, ApiArgs.Int(args, "x", worldXMin));
            int yMin = Math.Max(worldYMin, ApiArgs.Int(args, "y", worldYMin));
            int xMax = Math.Min(worldXMax, xMin + ApiArgs.Int(args, "w", worldXMax - worldXMin + 1) - 1);
            int yMax = Math.Min(worldYMax, yMin + ApiArgs.Int(args, "h", worldYMax - worldYMin + 1) - 1);
            if (xMax < xMin || yMax < yMin)
            {
                throw new ApiException(
                    "invalid_argument",
                    "The area is empty or outside the world.",
                    "World " + world.id + " spans x " + worldXMin + ".." + worldXMax + ", y " + worldYMin + ".." + worldYMax + ".");
            }

            int width = xMax - xMin + 1;
            int height = yMax - yMin + 1;
            int scale = Math.Max(1, Math.Max(CeilingDivide(width, MaxWidth), CeilingDivide(height, MaxHeight)));

            HashSet<int> duplicantCells = new HashSet<int>();
            foreach (MinionIdentity identity in Components.LiveMinionIdentities.Items)
            {
                if (identity != null)
                {
                    duplicantCells.Add(Grid.PosToCell(identity));
                }
            }

            Dictionary<string, object> layers = new Dictionary<string, object>();
            foreach (string layer in ReadLayerNames(args))
            {
                Dictionary<char, string> legend = new Dictionary<char, string>();
                string grid = Render(Renderer(layer, duplicantCells), xMin, yMin, xMax, yMax, scale, legend);
                layers[layer] = new Dictionary<string, object>
                {
                    { "grid", grid },
                    { "legend", legend.OrderBy(entry => entry.Key).ToDictionary(entry => entry.Key.ToString(), entry => entry.Value) }
                };
            }

            return new Dictionary<string, object>
            {
                { "world", world.id },
                { "x_min", xMin },
                { "x_max", xMax },
                { "y_min", yMin },
                { "y_max", yMax },
                { "cells_per_char", scale },
                { "layers", layers }
            };
        }

        private static List<string> ReadLayerNames(JObject args)
        {
            JToken requested = args["layers"];
            if (requested == null || requested.Type == JTokenType.Null)
            {
                return new List<string> { "terrain", "buildings" };
            }
            if (requested.Type != JTokenType.Array)
            {
                throw new ApiException("invalid_argument", "layers must be a list of layer names.");
            }
            return requested.Select(token => token.ToString()).ToList();
        }

        private static CellRenderer Renderer(string layer, HashSet<int> duplicantCells)
        {
            switch (layer)
            {
                case "terrain":
                    return Terrain;
                case "gas":
                    return Gas;
                case "liquid":
                    return Liquid;
                case "temperature":
                    return Temperature;
                case "buildings":
                    return (cell, legend) => Buildings(cell, legend, duplicantCells);
                case "utilities":
                    return Utilities;
                case "designations":
                    return Designations;
                default:
                    throw new ApiException(
                        "invalid_argument",
                        "No map layer is called " + layer + ".",
                        "Layers: terrain, buildings, utilities, gas, liquid, temperature, designations.");
            }
        }

        private static int CeilingDivide(int value, int divisor)
        {
            return (value + divisor - 1) / divisor;
        }

        private static string Render(
            CellRenderer renderer,
            int xMin,
            int yMin,
            int xMax,
            int yMax,
            int scale,
            Dictionary<char, string> legend)
        {
            StringBuilder text = new StringBuilder();
            const string Margin = "     ";
            text.Append(Margin);
            for (int x = xMin; x <= xMax; x += scale)
            {
                text.Append(x % 10 < scale ? (char)('0' + x / 10 % 10) : ' ');
            }
            text.Append('\n').Append(Margin);
            for (int x = xMin; x <= xMax; x += scale)
            {
                text.Append((char)('0' + x % 10));
            }
            text.Append('\n');

            Dictionary<char, int> votes = new Dictionary<char, int>();
            // The top row is the highest y, as on screen.
            for (int y = yMin + (yMax - yMin) / scale * scale; y >= yMin; y -= scale)
            {
                text.Append(y.ToString().PadLeft(4)).Append(' ');
                for (int x = xMin; x <= xMax; x += scale)
                {
                    votes.Clear();
                    for (int dy = 0; dy < scale && y + dy <= yMax; dy++)
                    {
                        for (int dx = 0; dx < scale && x + dx <= xMax; dx++)
                        {
                            char symbol = renderer(Grid.XYToCell(x + dx, y + dy), legend);
                            int count;
                            votes.TryGetValue(symbol, out count);
                            votes[symbol] = count + 1;
                        }
                    }
                    text.Append(votes.OrderByDescending(vote => vote.Value).ThenBy(vote => vote.Key).First().Key);
                }
                text.Append('\n');
            }
            return text.ToString();
        }

        private static char Mark(char symbol, string meaning, Dictionary<char, string> legend)
        {
            legend[symbol] = meaning;
            return symbol;
        }

        private static char Terrain(int cell, Dictionary<char, string> legend)
        {
            if (!Grid.IsVisible(cell))
            {
                return Mark(Unrevealed, "unrevealed", legend);
            }
            Element element = Grid.Element[cell];
            if (element.IsSolid)
            {
                string name = element.id.ToString();
                return Mark(SolidPalette.For(name), name, legend);
            }
            if (element.IsLiquid)
            {
                return Mark(AnyLiquid, "liquid", legend);
            }
            return element.IsGas ? Mark(AnyGas, "gas", legend) : Mark(Vacuum, "vacuum", legend);
        }

        private static char Gas(int cell, Dictionary<char, string> legend)
        {
            if (!Grid.IsVisible(cell))
            {
                return Mark(Unrevealed, "unrevealed", legend);
            }
            Element element = Grid.Element[cell];
            if (element.IsGas)
            {
                string name = element.id.ToString();
                return Mark(GasPalette.For(name), name, legend);
            }
            if (element.IsSolid)
            {
                return Mark(Solid, "solid", legend);
            }
            return element.IsLiquid ? Mark(AnyLiquid, "liquid", legend) : Mark(Vacuum, "vacuum", legend);
        }

        private static char Liquid(int cell, Dictionary<char, string> legend)
        {
            if (!Grid.IsVisible(cell))
            {
                return Mark(Unrevealed, "unrevealed", legend);
            }
            Element element = Grid.Element[cell];
            if (element.IsLiquid)
            {
                string name = element.id.ToString();
                return Mark(LiquidPalette.For(name), name, legend);
            }
            return element.IsSolid ? Mark(Solid, "solid", legend) : Mark(Vacuum, "no liquid", legend);
        }

        private static char Temperature(int cell, Dictionary<char, string> legend)
        {
            if (!Grid.IsVisible(cell))
            {
                return Mark(Unrevealed, "unrevealed", legend);
            }
            if (Grid.Element[cell].IsVacuum)
            {
                return Mark(Vacuum, "vacuum", legend);
            }
            float celsius = Grid.Temperature[cell] - 273.15f;
            int band = 0;
            while (band < TemperatureLimits.Length && celsius >= TemperatureLimits[band])
            {
                band++;
            }
            return Mark(TemperatureChars[band], TemperatureMeanings[band], legend);
        }

        private static char Buildings(int cell, Dictionary<char, string> legend, HashSet<int> duplicantCells)
        {
            if (duplicantCells.Contains(cell))
            {
                return Mark(Duplicant, "duplicant", legend);
            }
            foreach (ObjectLayer layer in BuildingLayers)
            {
                GameObject go = Grid.Objects[cell, (int)layer];
                if (go == null)
                {
                    continue;
                }
                Building building = go.GetComponent<Building>();
                string name = building?.Def != null ? building.Def.PrefabID : go.name;
                if (building is BuildingUnderConstruction)
                {
                    name += " (blueprint)";
                }
                return Mark(BuildingPalette.For(name), name, legend);
            }
            GameObject plant = Grid.Objects[cell, (int)ObjectLayer.Plants];
            if (plant != null)
            {
                string name = "plant " + plant.GetComponent<KPrefabID>().PrefabTag.Name;
                return Mark(BuildingPalette.For(name), name, legend);
            }
            return Grid.IsVisible(cell) ? Vacuum : Mark(Unrevealed, "unrevealed", legend);
        }

        private static char Utilities(int cell, Dictionary<char, string> legend)
        {
            char found = Vacuum;
            string meaning = null;
            foreach (KeyValuePair<ObjectLayer, char> layer in UtilityLayers)
            {
                if (Grid.Objects[cell, (int)layer.Key] == null)
                {
                    continue;
                }
                if (found != Vacuum)
                {
                    return Mark(Several, "several utilities (inspect for detail)", legend);
                }
                found = layer.Value;
                meaning = layer.Key.ToString();
            }
            return found == Vacuum ? Vacuum : Mark(found, meaning, legend);
        }

        private static char Designations(int cell, Dictionary<char, string> legend)
        {
            if (Grid.Objects[cell, (int)ObjectLayer.DigPlacer] != null)
            {
                return Mark('D', "dig", legend);
            }
            if (Grid.Objects[cell, (int)ObjectLayer.MopPlacer] != null)
            {
                return Mark('M', "mop", legend);
            }
            foreach (ObjectLayer layer in BuildingLayers)
            {
                GameObject go = Grid.Objects[cell, (int)layer];
                if (go == null)
                {
                    continue;
                }
                if (go.GetComponent<BuildingUnderConstruction>() != null)
                {
                    return Mark('B', "build", legend);
                }
                Deconstructable deconstructable = go.GetComponent<Deconstructable>();
                if (deconstructable != null && deconstructable.IsMarkedForDeconstruction())
                {
                    return Mark('X', "deconstruct", legend);
                }
            }
            return Vacuum;
        }
    }
}
