using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Patches;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// The game actions a player can take.  Legality is the game's own: placement
    /// goes through BuildingDef's checks and a refusal returns the game's reason.
    /// Nothing here decides what to do, and nothing is filtered by a list of
    /// allowed buildings or techs.
    /// </summary>
    internal static class ApiActions
    {
        private const int MaxAreaCells = 64 * 48;
        private const int MaxPathCells = 256;
        private const int DefaultPriority = 5;
        private const int LayerCount = (int)ObjectLayer.NumLayers;
        private const string DefaultFacade = "DEFAULT_FACADE";

        private const int TestSpeed = 4;
        private const int TestTimeScale = 10;
        private const int NormalTopTimeScale = 3;
        private const int MaxTimeScale = 60;
        private static readonly Regex CheckpointSuffix = new Regex("-ckpt-[0-9]{3}$", RegexOptions.CultureInvariant);

        internal static object Build(JObject args)
        {
            BuildingDef def = ReadBuildableDef(args);
            if (def.isUtility)
            {
                throw new ApiException("wrong_tool", def.PrefabID + " is laid along a path.", "Use build_path.");
            }
            int cell = ReadCell(args);
            WorldContainer world = WorldForCell(cell);
            Orientation orientation = ReadOrientation(args, def);
            List<Tag> materials = ReadMaterials(args, def, world);
            int priority = ReadPriority(args);

            if (ApiRocketTools.IsRocketModuleDef(def))
            {
                GameObject rocketModule = ApiRocketTools.PlaceRocketModule(def, cell, materials);
                int moduleCell = Grid.PosToCell(rocketModule);
                ApiEvents.ExpectBuilding(def.PrefabID, moduleCell);
                SetPriority(rocketModule, priority);
                return new Dictionary<string, object>
                {
                    { "id", rocketModule.GetComponent<KPrefabID>().InstanceID },
                    { "def", def.PrefabID },
                    { "x", XOf(moduleCell) },
                    { "y", YOf(moduleCell) },
                    { "materials", materials.Select(material => material.Name).ToList() },
                    { "priority", priority }
                };
            }

            string failure;
            if (!def.IsValidPlaceLocation(null, cell, orientation, false, out failure, false))
            {
                throw new ApiException(
                    "invalid_placement",
                    def.PrefabID + " cannot go at " + Describe(cell) + ": " + failure,
                    "Footprint " + def.WidthInCells + "x" + def.HeightInCells + ", rule " + def.BuildLocationRule +
                    ". Check the area with get_map.");
            }

            GameObject blueprint = Place(def, cell, orientation, materials);
            SetPriority(blueprint, priority);
            return new Dictionary<string, object>
            {
                { "id", blueprint.GetComponent<KPrefabID>().InstanceID },
                { "def", def.PrefabID },
                { "x", XOf(cell) },
                { "y", YOf(cell) },
                { "materials", materials.Select(material => material.Name).ToList() },
                { "priority", priority }
            };
        }

        internal static object BuildPath(JObject args)
        {
            BuildingDef def = ReadBuildableDef(args);
            if (!def.isUtility && (def.WidthInCells != 1 || def.HeightInCells != 1))
            {
                throw new ApiException("wrong_tool", def.PrefabID + " is not laid along a path.", "Use build.");
            }
            List<int> cells = ReadPath(args);
            WorldContainer world = WorldForCell(cells[0]);
            if (cells.Any(c => Grid.WorldIdx[c] != world.id))
            {
                throw new ApiException("invalid_placement", "A single build_path cannot cross between different worlds.");
            }
            List<Tag> materials = ReadMaterials(args, def, world);
            int priority = ReadPriority(args);
            if (!def.isUtility)
            {
                return BuildLine(def, cells, materials, priority);
            }

            IUtilityNetworkMgr manager = def.BuildingComplete.GetComponent<IHaveUtilityNetworkMgr>()?.GetNetworkManager();
            if (manager == null)
            {
                throw new ApiException("internal_error", def.PrefabID + " has no utility network.");
            }

            // Cells already holding this utility join the path, as a drag in the
            // game does: that is how a new branch connects to an existing line.
            GameObject[] present = new GameObject[cells.Count];
            for (int index = 0; index < cells.Count; index++)
            {
                present[index] = Grid.Objects[cells[index], (int)def.TileLayer];
                string failure;
                if (present[index] == null &&
                    !def.IsValidPlaceLocation(null, cells[index], Orientation.Neutral, false, out failure, false))
                {
                    throw new ApiException(
                        "invalid_placement",
                        def.PrefabID + " cannot go at " + Describe(cells[index]) + ": " + failure,
                        "Nothing was placed. Check the path with get_map.");
                }
            }

            List<GameObject> created = new List<GameObject>();
            try
            {
                for (int index = 0; index < cells.Count; index++)
                {
                    if (present[index] == null)
                    {
                        present[index] = Place(def, cells[index], Orientation.Neutral, materials);
                        created.Add(present[index]);
                        SetPriority(present[index], priority);
                    }
                }
            }
            catch (ApiException)
            {
                Cancel(created);
                throw;
            }

            // The same order as BaseUtilityBuildTool.BuildPath: register the path's
            // adjacency, then give every cell its connections.  Without this each
            // cell finishes as its own one-cell network.
            for (int index = 1; index < cells.Count; index++)
            {
                UtilityConnections direction = UtilityConnectionsExtensions.DirectionFromToCell(cells[index - 1], cells[index]);
                manager.AddConnection(direction, cells[index - 1], is_physical_building: false);
                manager.AddConnection(direction.InverseDirection(), cells[index], is_physical_building: false);
            }
            for (int index = 0; index < cells.Count; index++)
            {
                IUtilityItem item = present[index].GetComponent<KAnimGraphTileVisualizer>();
                UtilityConnections connections = manager.GetConnections(cells[index], is_physical_building: false);
                if (item != null)
                {
                    connections |= item.Connections;
                    if (present[index].GetComponent<BuildingComplete>() != null)
                    {
                        item.UpdateConnections(connections);
                    }
                    item.Connections = connections;
                }
                TileVisualizer.RefreshCell(cells[index], def.TileLayer, def.ReplacementLayer);
            }

            return new Dictionary<string, object>
            {
                { "def", def.PrefabID },
                { "cells", cells.Count },
                { "placed", created.Count },
                { "joined_existing", cells.Count - created.Count },
                { "materials", materials.Select(material => material.Name).ToList() }
            };
        }

        // Ladders and tiles: no network to join, so one blueprint per cell.
        private static object BuildLine(BuildingDef def, List<int> cells, List<Tag> materials, int priority)
        {
            List<int> open = new List<int>();
            foreach (int cell in cells)
            {
                GameObject present = Grid.Objects[cell, (int)def.ObjectLayer];
                if (present != null && present.PrefabID().Name.StartsWith(def.PrefabID, StringComparison.Ordinal))
                {
                    continue;
                }
                string failure;
                if (!def.IsValidPlaceLocation(null, cell, Orientation.Neutral, false, out failure, false))
                {
                    throw new ApiException(
                        "invalid_placement",
                        def.PrefabID + " cannot go at " + Describe(cell) + ": " + failure,
                        "Nothing was placed. Check the path with get_map.");
                }
                open.Add(cell);
            }

            List<GameObject> created = new List<GameObject>();
            try
            {
                foreach (int cell in open)
                {
                    GameObject blueprint = Place(def, cell, Orientation.Neutral, materials);
                    created.Add(blueprint);
                    SetPriority(blueprint, priority);
                }
            }
            catch (ApiException)
            {
                Cancel(created);
                throw;
            }
            return new Dictionary<string, object>
            {
                { "def", def.PrefabID },
                { "cells", cells.Count },
                { "placed", created.Count },
                { "materials", materials.Select(tag => tag.Name).ToList() }
            };
        }

        internal static object Dig(JObject args)
        {
            List<int> cells = ReadArea(args);
            int priority = ReadPriority(args);
            int marked = 0;
            int alreadyMarked = 0;
            int notSolid = 0;
            int undiggable = 0;
            int occupied = 0;
            foreach (int cell in cells)
            {
                if (Grid.Objects[cell, (int)ObjectLayer.DigPlacer] != null)
                {
                    alreadyMarked++;
                }
                else if (!Grid.Solid[cell] || Grid.Foundation[cell])
                {
                    notSolid++;
                }
                else if (Grid.Element[cell].hardness >= byte.MaxValue)
                {
                    undiggable++;
                }
                else
                {
                    GameObject placer = DigTool.PlaceDig(cell, marked);
                    if (placer == null)
                    {
                        occupied++;
                        continue;
                    }
                    SetPriority(placer, priority);
                    marked++;
                }
            }
            return new Dictionary<string, object>
            {
                { "marked", marked },
                { "already_marked", alreadyMarked },
                { "not_solid", notSolid },
                { "undiggable", undiggable },
                { "occupied_by_blueprint", occupied }
            };
        }

        internal static object Deconstruct(JObject args)
        {
            int priority = ReadPriority(args);
            HashSet<GameObject> targets = ReadTargets(args);
            bool byId = args["id"] != null;
            int marked = 0;
            int protectedCount = 0;
            int notDeconstructable = 0;
            foreach (GameObject target in targets)
            {
                if (target.GetComponent<KPrefabID>().HasTag(GameTags.Telepad))
                {
                    if (byId)
                    {
                        throw new ApiException("forbidden", "The printing pod may not be deconstructed.");
                    }
                    protectedCount++;
                    continue;
                }
                Deconstructable deconstructable = target.GetComponent<Deconstructable>();
                if (deconstructable == null)
                {
                    notDeconstructable++;
                    continue;
                }
                target.Trigger((int)GameHashes.MarkForDeconstruct);
                if (deconstructable.IsMarkedForDeconstruction())
                {
                    SetPriority(target, priority);
                    marked++;
                }
                else
                {
                    notDeconstructable++;
                }
            }
            return new Dictionary<string, object>
            {
                { "marked", marked },
                { "protected", protectedCount },
                { "not_deconstructable", notDeconstructable }
            };
        }

        internal static object CancelArea(JObject args)
        {
            int blueprints = 0;
            int deconstructions = 0;
            HashSet<GameObject> targets = new HashSet<GameObject>();
            foreach (int cell in ReadArea(args))
            {
                AddCellObjects(cell, targets);
            }
            int digs = 0;
            foreach (GameObject target in targets)
            {
                if (target.GetComponent<Diggable>() != null)
                {
                    digs++;
                }
                else if (target.GetComponent<Constructable>() != null)
                {
                    blueprints++;
                }
                else if (target.GetComponent<Deconstructable>()?.IsMarkedForDeconstruction() == true)
                {
                    deconstructions++;
                }
                else
                {
                    continue;
                }
                target.Trigger((int)GameHashes.Cancel);
            }
            return new Dictionary<string, object>
            {
                { "digs", digs },
                { "blueprints", blueprints },
                { "deconstructions", deconstructions }
            };
        }

        internal static object SetPriorityTool(JObject args)
        {
            int level = ReadPriority(args);
            int changed = 0;
            foreach (GameObject target in ReadTargets(args))
            {
                if (target.GetComponent<Prioritizable>() != null)
                {
                    SetPriority(target, level);
                    changed++;
                }
            }
            return new Dictionary<string, object>
            {
                { "changed", changed },
                { "priority", level }
            };
        }

        internal static object SetResearch(JObject args)
        {
            string id = ApiArgs.Text(args, "tech");
            Tech tech = string.IsNullOrEmpty(id) ? null : Db.Get().Techs.TryGet(id);
            if (tech == null)
            {
                throw new ApiException("unknown_tech", "No tech is called " + id + ".", "get_research lists the techs.");
            }
            if (tech.IsComplete())
            {
                throw new ApiException("already_complete", id + " is already researched.");
            }
            if (ApiArgs.Flag(args, "require_ready", false) && !tech.ArePrerequisitesComplete())
            {
                throw new ApiException(
                    "prerequisites_incomplete",
                    id + " still needs: " + string.Join(
                        ", ",
                        tech.requiredTech.Where(required => !required.IsComplete()).Select(required => required.Id)) + ".",
                    "Leave require_ready off to queue the prerequisites first.");
            }

            // With the queue cleared, the game queues every unfinished prerequisite.
            Research.Instance.SetActiveResearch(tech, true);
            return new Dictionary<string, object>
            {
                { "current", Research.Instance.GetActiveResearch()?.tech?.Id },
                { "queue", Research.Instance.GetResearchQueue().Select(item => item.tech.Id).ToList() },
                { "missing_research_building", ApiCatalogReader.MissingResearchBuilding() }
            };
        }

        internal static object SetSpeed(JObject args)
        {
            int speed = ApiArgs.Int(args, "speed", -1);
            if (speed < 0 || speed > TestSpeed)
            {
                throw new ApiException("invalid_argument", "speed must be 0 (pause), 1, 2, 3 or 4 (10x, for long tests).");
            }
            int timeScale = ReadTimeScale(args, speed);

            SpeedControlScreen control = SpeedControlScreen.Instance;
            if (speed == 0)
            {
                if (!control.IsPaused)
                {
                    control.Pause(false, false);
                }
            }
            else
            {
                // Speed 4 is the game's own debug speed: its top setting at ten times,
                // or at time_scale when one is given.
                control.ultraSpeed = speed == TestSpeed ? timeScale : NormalTopTimeScale;
                control.SetSpeed(0);
                control.SetSpeed(Math.Min(speed, 3) - 1);
                if (control.IsPaused)
                {
                    control.Unpause(false);
                }
                if (control.IsPaused)
                {
                    // The game counts pauses, and an open dialog holds one of its own.
                    throw new ApiException(
                        "still_paused",
                        "The speed was set, but something else is also holding the game paused.",
                        "Call set_speed again; if it stays paused, a dialog is open.");
                }
            }
            return new Dictionary<string, object>
            {
                { "paused", control.IsPaused },
                { "speed", speed == 0 ? control.GetSpeed() + 1 : speed },
                { "time_scale", Time.timeScale }
            };
        }

        private static int ReadTimeScale(JObject args, int speed)
        {
            int timeScale = ApiArgs.Int(args, "time_scale", -1);
            if (timeScale == -1)
            {
                return TestTimeScale;
            }
            if (speed != TestSpeed)
            {
                throw new ApiException("invalid_argument", "time_scale only goes with speed 4.");
            }
            if (timeScale <= NormalTopTimeScale || timeScale > MaxTimeScale)
            {
                throw new ApiException(
                    "invalid_argument",
                    "time_scale must be from " + (NormalTopTimeScale + 1) + " to " + MaxTimeScale + ".");
            }
            if (timeScale > TestTimeScale && !SimFrameLimitPatch.IsInstalled)
            {
                throw new ApiException(
                    "speed_limit_in_place",
                    "The game's limit per frame could not be lifted, so a time_scale above " + TestTimeScale + " would not take effect.",
                    "Use time_scale " + TestTimeScale + " or lower.");
            }
            return timeScale;
        }

        // A new file next to the active save, never an existing one, and the game
        // keeps pointing at the save it loaded.  The manifest that makes it part of
        // a checkpoint chain is written outside the game.
        internal static object SaveCheckpoint(JObject args)
        {
            string active = Path.GetFullPath(SaveLoader.GetActiveSaveFilePath());
            string folder = Path.GetDirectoryName(active);
            string stem = CheckpointSuffix.Replace(Path.GetFileNameWithoutExtension(active), string.Empty);
            int index = 1;
            string target;
            do
            {
                target = Path.Combine(folder, stem + "-ckpt-" + index.ToString("000") + ".sav");
                index++;
            }
            while (File.Exists(target));

            string written = SaveLoader.Instance.Save(target, false, false);
            FileInfo file = new FileInfo(target);
            if (!file.Exists || file.Length == 0)
            {
                throw new ApiException("save_failed", "The game did not write " + target + ".");
            }
            string note = ApiArgs.Text(args, "note");
            ApiEvents.Add("checkpoint_saved", new Dictionary<string, object>
            {
                { "path", written },
                { "note", note }
            });
            return new Dictionary<string, object>
            {
                { "path", written },
                { "bytes", file.Length },
                { "note", note }
            };
        }

        private static GameObject Place(BuildingDef def, int cell, Orientation orientation, List<Tag> materials)
        {
            GameObject blueprint = def.TryPlace(
                null,
                Grid.CellToPosCBC(cell, def.SceneLayer),
                orientation,
                materials,
                DefaultFacade,
                restrictToActiveWorld: false);
            if (blueprint == null)
            {
                throw new ApiException("invalid_placement", "The game placed no " + def.PrefabID + " at " + Describe(cell) + ".");
            }
            ApiEvents.ExpectBuilding(def.PrefabID, cell);
            return blueprint;
        }

        private static void Cancel(IEnumerable<GameObject> blueprints)
        {
            foreach (GameObject blueprint in blueprints)
            {
                if (blueprint != null)
                {
                    blueprint.Trigger((int)GameHashes.Cancel);
                }
            }
        }

        internal static void SetPriority(GameObject target, int level)
        {
            Prioritizable prioritizable = target.GetComponent<Prioritizable>();
            if (prioritizable != null)
            {
                prioritizable.SetMasterPriority(new PrioritySetting(PriorityScreen.PriorityClass.basic, level));
            }
        }

        private static BuildingDef ReadBuildableDef(JObject args)
        {
            string id = ApiArgs.Text(args, "def_id");
            BuildingDef def = string.IsNullOrEmpty(id) ? null : Assets.GetBuildingDef(id);
            if (def == null || !def.IsAvailable() || !Game.IsCorrectDlcActiveForCurrentSave(def))
            {
                throw new ApiException("unknown_building", "No buildable building is called " + id + ".", "get_buildable lists them.");
            }
            if (!Db.Get().TechItems.IsTechItemComplete(def.PrefabID))
            {
                string tech = Db.Get().TechItems.GetTechFromItemID(def.PrefabID)?.Id;
                throw new ApiException("not_researched", id + " is locked.", "Research " + tech + " first.");
            }
            return def;
        }

        private static WorldContainer WorldForCell(int cell)
        {
            byte worldId = Grid.WorldIdx[cell];
            WorldContainer world = worldId == byte.MaxValue ? null : ClusterManager.Instance.GetWorld(worldId);
            if (world == null)
            {
                throw new ApiException("invalid_placement", Describe(cell) + " is not inside any active asteroid or rocket world.");
            }
            return world;
        }

        private static List<Tag> ReadMaterials(JObject args, BuildingDef def, WorldContainer world)
        {
            Recipe.Ingredient[] ingredients = def.CraftRecipe.Ingredients.ToArray();
            JToken requested = args["materials"];
            JArray names = requested as JArray;
            if (requested != null && requested.Type != JTokenType.Null &&
                (names == null || names.Count != ingredients.Length))
            {
                throw new ApiException(
                    "invalid_argument",
                    "materials must list one element for each of " + def.PrefabID + "'s " + ingredients.Length + " ingredient(s).");
            }

            List<Tag> selected = new List<Tag>();
            for (int index = 0; index < ingredients.Length; index++)
            {
                Recipe.Ingredient ingredient = ingredients[index];
                List<Tag> valid = MaterialSelector.GetValidMaterials(ingredient.tag, omitDisabledElements: true);
                Tag choice;
                if (names != null)
                {
                    string name = names[index].ToString();
                    choice = valid.FirstOrDefault(tag => string.Equals(tag.Name, name, StringComparison.OrdinalIgnoreCase));
                    if (!choice.IsValid)
                    {
                        throw new ApiException(
                            "invalid_material",
                            name + " cannot be used as " + ingredient.tag.Name + ".",
                            DescribeOptions(valid, world));
                    }
                }
                else
                {
                    choice = valid
                        .OrderByDescending(tag => world.worldInventory.GetAmount(tag, includeRelatedWorlds: true))
                        .ThenBy(tag => tag.Name, StringComparer.Ordinal)
                        .FirstOrDefault();
                }

                float available = choice.IsValid ? world.worldInventory.GetAmount(choice, includeRelatedWorlds: true) : 0f;
                if (available < ingredient.amount)
                {
                    throw new ApiException(
                        "material_insufficient",
                        "Needs " + ingredient.amount + " kg of " + ingredient.tag.Name + "; the best choice has " +
                        Math.Round(available, 1) + " kg.",
                        DescribeOptions(valid, world));
                }
                selected.Add(choice);
            }
            return selected;
        }

        private static string DescribeOptions(IEnumerable<Tag> valid, WorldContainer world)
        {
            List<string> options = valid
                .Select(tag => new KeyValuePair<string, float>(tag.Name, world.worldInventory.GetAmount(tag, includeRelatedWorlds: true)))
                .Where(option => option.Value > 0f)
                .OrderByDescending(option => option.Value)
                .Take(5)
                .Select(option => option.Key + " " + Math.Round(option.Value, 0) + " kg")
                .ToList();
            return options.Count == 0 ? "The colony has none of the valid materials." : "Available: " + string.Join(", ", options);
        }

        private static Orientation ReadOrientation(JObject args, BuildingDef def)
        {
            string name = ApiArgs.Text(args, "rotation");
            if (name == null)
            {
                return Orientation.Neutral;
            }
            Orientation orientation;
            if (!Enum.TryParse(name, false, out orientation) || orientation == Orientation.NumRotations)
            {
                throw new ApiException("invalid_argument", "rotation must be Neutral, R90, R180, R270, FlipH or FlipV.");
            }

            bool permitted;
            switch (def.PermittedRotations)
            {
                case PermittedRotations.R360:
                    permitted = orientation < Orientation.NumRotations;
                    break;
                case PermittedRotations.R90:
                    permitted = orientation == Orientation.Neutral || orientation == Orientation.R90;
                    break;
                case PermittedRotations.FlipH:
                    permitted = orientation == Orientation.Neutral || orientation == Orientation.FlipH;
                    break;
                case PermittedRotations.FlipV:
                    permitted = orientation == Orientation.Neutral || orientation == Orientation.FlipV;
                    break;
                default:
                    permitted = orientation == Orientation.Neutral;
                    break;
            }
            if (!permitted)
            {
                throw new ApiException(
                    "invalid_rotation",
                    def.PrefabID + " cannot take rotation " + name + ".",
                    "It allows: " + def.PermittedRotations + ".");
            }
            return orientation;
        }

        internal static int ReadPriority(JObject args)
        {
            int level = ApiArgs.Int(args, "priority", DefaultPriority);
            if (level < 1 || level > 9)
            {
                throw new ApiException("invalid_argument", "priority must be between 1 and 9.");
            }
            return level;
        }

        private static int ReadCell(JObject args)
        {
            int x = ApiArgs.Int(args, "x", int.MinValue);
            int y = ApiArgs.Int(args, "y", int.MinValue);
            if (x == int.MinValue || y == int.MinValue)
            {
                throw new ApiException("invalid_argument", "x and y are required.");
            }
            return ApiValues.CheckedCell(x, y);
        }

        internal static List<int> ReadArea(JObject args)
        {
            int cell = ReadCell(args);
            int width = ApiArgs.Int(args, "w", 1);
            int height = ApiArgs.Int(args, "h", 1);
            if (width < 1 || height < 1 || (long)width * height > MaxAreaCells)
            {
                throw new ApiException("invalid_argument", "w and h must be positive, at most " + MaxAreaCells + " cells in all.");
            }
            int left = XOf(cell);
            int bottom = YOf(cell);
            List<int> cells = new List<int>(width * height);
            for (int y = bottom; y < bottom + height; y++)
            {
                for (int x = left; x < left + width; x++)
                {
                    cells.Add(ApiValues.CheckedCell(x, y));
                }
            }
            return cells;
        }

        private static List<int> ReadPath(JObject args)
        {
            JArray points = args["cells"] as JArray;
            if (points == null || points.Count == 0 || points.Count > MaxPathCells)
            {
                throw new ApiException("invalid_argument", "cells must list 1 to " + MaxPathCells + " [x, y] pairs.");
            }

            List<int> cells = new List<int>(points.Count);
            foreach (JToken point in points)
            {
                JArray pair = point as JArray;
                if (pair == null || pair.Count != 2 || pair[0].Type != JTokenType.Integer || pair[1].Type != JTokenType.Integer)
                {
                    throw new ApiException("invalid_argument", "Each cell must be an [x, y] pair of integers.");
                }
                int cell = ApiValues.CheckedCell(pair[0].Value<int>(), pair[1].Value<int>());
                if (cells.Contains(cell))
                {
                    throw new ApiException("invalid_argument", "The path visits " + Describe(cell) + " twice.");
                }
                if (cells.Count > 0 && UtilityConnectionsExtensions.DirectionFromToCell(cells[cells.Count - 1], cell) == 0)
                {
                    throw new ApiException(
                        "invalid_argument",
                        Describe(cells[cells.Count - 1]) + " and " + Describe(cell) + " are not neighbours.",
                        "A path moves one cell up, down, left or right at each step.");
                }
                cells.Add(cell);
            }
            return cells;
        }

        // A target is one building by id, or everything in an area.
        private static HashSet<GameObject> ReadTargets(JObject args)
        {
            HashSet<GameObject> targets = new HashSet<GameObject>();
            if (args["id"] != null)
            {
                targets.Add(FindById(ApiArgs.Int(args, "id", 0)));
                return targets;
            }
            foreach (int cell in ReadArea(args))
            {
                AddCellObjects(cell, targets);
            }
            return targets;
        }

        internal static GameObject FindById(int id)
        {
            KPrefabID prefab = KPrefabIDTracker.Get().GetInstance(id);
            if (prefab == null)
            {
                throw new ApiException(
                    "unknown_target",
                    "Nothing has id " + id + ".",
                    "A finished building has a new id, different from its blueprint's: use list_buildings or inspect.");
            }
            return prefab.gameObject;
        }

        private static void AddCellObjects(int cell, HashSet<GameObject> targets)
        {
            for (int layer = 0; layer < LayerCount; layer++)
            {
                GameObject go = Grid.Objects[cell, layer];
                if (go != null)
                {
                    targets.Add(go);
                }
            }
        }

        private static int XOf(int cell)
        {
            int x;
            int y;
            Grid.CellToXY(cell, out x, out y);
            return x;
        }

        private static int YOf(int cell)
        {
            int x;
            int y;
            Grid.CellToXY(cell, out x, out y);
            return y;
        }

        private static string Describe(int cell)
        {
            return "(" + XOf(cell) + ", " + YOf(cell) + ")";
        }
    }

    /// <summary>
    /// One line per call, for reading a run back afterwards.  It is a record,
    /// not a gate: nothing reads it during play.
    /// </summary>
    internal static class ApiAudit
    {
        private const string FileName = "api-audit.jsonl";

        internal static void Record(string tool, JObject args, JObject response, bool writes)
        {
            try
            {
                JObject line = new JObject
                {
                    ["utc"] = DateTimeOffset.UtcNow.ToString("o"),
                    ["tool"] = tool,
                    ["args"] = args,
                    ["ok"] = response["ok"]
                };
                if (GameClock.Instance != null)
                {
                    line["cycle"] = Math.Round(GameClock.Instance.GetTimeInCycles() + 1.0, 3);
                }
                if (response["error"] != null)
                {
                    line["error"] = response["error"];
                }
                else if (writes)
                {
                    line["result"] = response["result"];
                }
                File.AppendAllText(
                    Path.Combine(ApiLog.BridgeDirectory, FileName),
                    line.ToString(Newtonsoft.Json.Formatting.None) + "\n");
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                // A full disk must not stop the game; the player still has the response.
                Debug.LogWarning(Mod.LogPrefix + " Audit line lost: " + ex.Message);
            }
        }
    }
}
