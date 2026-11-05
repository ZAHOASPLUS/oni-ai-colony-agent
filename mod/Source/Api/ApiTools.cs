using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;

namespace OniAiProbeMod.Api
{
    /// <summary>A refusal the caller can act on; it becomes the response's error object.</summary>
    internal sealed class ApiException : Exception
    {
        internal ApiException(string code, string message, string hint = null, JObject details = null)
            : base(message)
        {
            Code = code;
            Hint = hint;
            Details = details;
        }

        internal string Code { get; }

        internal string Hint { get; }

        /// <summary>More fields for the error object, such as the ids an ambiguous name could mean.</summary>
        internal JObject Details { get; }
    }

    /// <summary>
    /// Tool dispatch, on the game's main thread.  The tool list here and
    /// tools/oni-tools.json must name the same tools.
    /// </summary>
    internal static class ApiTools
    {
        private sealed class Tool
        {
            internal Tool(Func<JObject, object> handler, bool needsGame, bool writes = false)
            {
                Handler = handler;
                NeedsGame = needsGame;
                Writes = writes;
            }

            internal Func<JObject, object> Handler { get; }

            internal bool NeedsGame { get; }

            internal bool Writes { get; }
        }

        private static readonly Dictionary<string, Tool> Tools = new Dictionary<string, Tool>(StringComparer.Ordinal)
        {
            { "get_lifecycle", new Tool(_ => ApiLifecycle.Describe(), false) },
            { "load_save", new Tool(ApiLifecycle.LoadSaveTool, false) },
            { "get_status", new Tool(ApiStatusReader.Read, true) },
            { "get_map", new Tool(ApiMapReader.Read, true) },
            { "inspect", new Tool(ApiInspectReader.Read, true) },
            { "get_buildable", new Tool(ApiCatalogReader.ReadBuildable, true) },
            { "get_research", new Tool(ApiCatalogReader.ReadResearch, true) },
            { "get_events", new Tool(ApiEvents.Read, true) },
            { "list_buildings", new Tool(ApiQueries.ListBuildings, true) },
            { "get_resources", new Tool(ApiQueries.ReadResources, true) },
            { "find", new Tool(ApiQueries.Find, true) },
            { "get_progress", new Tool(ApiQueries.ReadProgress, true) },
            { "build", new Tool(ApiActions.Build, true, true) },
            { "build_path", new Tool(ApiActions.BuildPath, true, true) },
            { "dig", new Tool(ApiActions.Dig, true, true) },
            { "deconstruct", new Tool(ApiActions.Deconstruct, true, true) },
            { "cancel", new Tool(ApiActions.CancelArea, true, true) },
            { "set_priority", new Tool(ApiActions.SetPriorityTool, true, true) },
            { "set_research", new Tool(ApiActions.SetResearch, true, true) },
            { "set_speed", new Tool(ApiActions.SetSpeed, true, true) },
            { "harvest", new Tool(ApiPlayTools.Harvest, true, true) },
            { "sweep", new Tool(ApiPlayTools.Sweep, true, true) },
            { "mop", new Tool(ApiPlayTools.Mop, true, true) },
            { "configure", new Tool(ApiPlayTools.Configure, true, true) },
            { "set_chore_priority", new Tool(ApiPlayTools.SetChorePriority, true, true) },
            { "set_schedule", new Tool(ApiPlayTools.SetSchedule, true, true) },
            { "get_skills", new Tool(ApiSkills.Read, true) },
            { "learn_skill", new Tool(ApiSkills.Learn, true, true) },
            { "move_to", new Tool(ApiMovement.MoveTo, true, true) },
            { "get_printables", new Tool(ApiPrintables.Read, true) },
            { "choose_printable", new Tool(ApiPrintables.Choose, true, true) },
            { "reject_printables", new Tool(ApiPrintables.Reject, true, true) },
            { "save_checkpoint", new Tool(ApiActions.SaveCheckpoint, true, true) }
        };

        internal static JObject Invoke(string name, JObject args)
        {
            Tool tool;
            Tools.TryGetValue(name, out tool);
            bool writes = tool != null && tool.Writes;
            JObject response = Run(name, tool, args);
            ApiAudit.Record(name, args, response, writes);
            if (writes && response["error"] != null)
            {
                ApiEvents.Add("action_failed", new Dictionary<string, object>
                {
                    { "tool", name },
                    { "code", (string)response["error"]["code"] }
                });
            }
            return response;
        }

        private static JObject Run(string name, Tool tool, JObject args)
        {
            try
            {
                if (tool == null)
                {
                    throw new ApiException(
                        "unknown_tool",
                        "No tool is called " + name + ".",
                        "Tools: " + string.Join(", ", Tools.Keys.OrderBy(key => key, StringComparer.Ordinal)));
                }
                if (tool.NeedsGame && ApiLifecycle.State() != ApiLifecycle.InGame)
                {
                    throw new ApiException(
                        "game_not_loaded",
                        name + " needs a loaded colony; the game is at " + ApiLifecycle.State() + ".",
                        "Call get_lifecycle until the state is in_game.");
                }
                if (tool.Writes)
                {
                    RequireWritableSession();
                }
                return ApiResult.Success(tool.Handler(args));
            }
            catch (ApiException ex)
            {
                return Refusal(ex);
            }
            catch (Exception ex)
            {
                // Never let a tool's failure reach the game's update loop.
                ApiLog.Error("api_" + name, ex);
                return ApiResult.Failure("internal_error", ex.GetType().Name + ": " + ex.Message);
            }
        }

        internal static JObject Refusal(ApiException ex)
        {
            JObject response = ApiResult.Failure(ex.Code, ex.Message, ex.Hint);
            if (ex.Details != null)
            {
                JObject error = (JObject)response["error"];
                foreach (JProperty field in ex.Details.Properties())
                {
                    error[field.Name] = field.Value;
                }
            }
            return response;
        }

        // The hard limits on any change to the game: only a test clone, and only
        // while autosave cannot rotate anyone's saves away.
        private static void RequireWritableSession()
        {
            if (!ApiSaveGuard.IsClone(SaveLoader.GetActiveSaveFilePath()))
            {
                throw new ApiException("writes_refused", "The loaded save is not a test clone; it is read-only.");
            }
            if (!AutosaveGuard.IsActive)
            {
                throw new ApiException("writes_refused", "The autosave guard is not active.");
            }
        }
    }

    internal static class ApiArgs
    {
        internal static int Int(JObject args, string name, int fallback)
        {
            JToken value = args[name];
            if (value == null || value.Type == JTokenType.Null)
            {
                return fallback;
            }
            if (value.Type != JTokenType.Integer)
            {
                throw new ApiException("invalid_argument", name + " must be an integer.");
            }
            return value.Value<int>();
        }

        internal static string Text(JObject args, string name)
        {
            JToken value = args[name];
            if (value == null || value.Type == JTokenType.Null)
            {
                return null;
            }
            if (value.Type != JTokenType.String)
            {
                throw new ApiException("invalid_argument", name + " must be a string.");
            }
            return value.Value<string>();
        }

        internal static bool Flag(JObject args, string name, bool fallback)
        {
            JToken value = args[name];
            if (value == null || value.Type == JTokenType.Null)
            {
                return fallback;
            }
            if (value.Type != JTokenType.Boolean)
            {
                throw new ApiException("invalid_argument", name + " must be true or false.");
            }
            return value.Value<bool>();
        }
    }

    internal static class ApiValues
    {
        // JSON has no NaN or Infinity, and the game can hold either in a sim value.
        internal static object Number(float value, int digits = 2)
        {
            if (float.IsNaN(value) || float.IsInfinity(value))
            {
                return null;
            }
            return Math.Round((double)value, digits);
        }

        internal static object Celsius(float kelvin)
        {
            return Number(kelvin - 273.15f, 1);
        }

        internal static WorldContainer World(JObject args)
        {
            ClusterManager manager = ClusterManager.Instance;
            int id = ApiArgs.Int(args, "world", manager.activeWorldId);
            WorldContainer world = manager.GetWorld(id);
            if (world == null)
            {
                throw new ApiException("invalid_argument", "No world has id " + id + ".", "get_status lists the worlds.");
            }
            return world;
        }

        // Grid.XYToCell does no checking: an x past the edge wraps to the next row.
        internal static int CheckedCell(int x, int y)
        {
            if (x < 0 || y < 0 || x >= Grid.WidthInCells || y >= Grid.HeightInCells)
            {
                throw new ApiException("invalid_argument", "(" + x + ", " + y + ") is outside the map.");
            }
            return Grid.XYToCell(x, y);
        }

        internal static Dictionary<string, object> Xy(int cell)
        {
            int x;
            int y;
            Grid.CellToXY(cell, out x, out y);
            return new Dictionary<string, object> { { "x", x }, { "y", y } };
        }
    }

    internal static class ApiLifecycle
    {
        internal const string MainMenu = "main_menu";
        internal const string Loading = "loading";
        internal const string InGame = "in_game";

        internal static bool SessionActive { get; set; }

        internal static string State()
        {
            if (Game.Instance != null && SpeedControlScreen.Instance != null && SessionActive)
            {
                return InGame;
            }
            return global::MainMenu.Instance != null ? MainMenu : Loading;
        }

        internal static object Describe()
        {
            string state = State();
            return new Dictionary<string, object>
            {
                { "state", state },
                { "save", state == InGame ? SaveLoader.GetActiveSaveFilePath() : null },
                { "autoload_pending", !string.IsNullOrEmpty(ApiMode.PendingSave) }
            };
        }

        internal static object LoadSaveTool(JObject args)
        {
            if (State() != MainMenu)
            {
                throw new ApiException(
                    "wrong_state",
                    "load_save only works at the main menu; the game is at " + State() + ".");
            }
            string path = ApiArgs.Text(args, "path");
            Load(path);
            return new Dictionary<string, object> { { "loading", path } };
        }

        // The same steps as MainMenu.ResumeGame, with the save chosen by the caller.
        internal static void Load(string path)
        {
            string refusal = ApiSaveGuard.Refusal(path);
            if (refusal != null)
            {
                throw new ApiException("save_refused", refusal);
            }
            ApiEvents.Add("load_requested", new Dictionary<string, object> { { "save", path } });
            KCrashReporter.MOST_RECENT_SAVEFILE = path;
            SaveLoader.SetActiveSaveFilePath(path);
            LoadingOverlay.Load(SaveLoader.LoadScene);
        }
    }
}
