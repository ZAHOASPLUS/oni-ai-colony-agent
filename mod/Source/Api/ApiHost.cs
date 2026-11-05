using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Probe;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>
    /// Runs tool calls on the game's main thread.  It lives from Mod.OnLoad to
    /// process exit, because the main menu needs it too.
    /// </summary>
    public sealed class ApiHost : MonoBehaviour
    {
        internal const string ApiFileName = "api.json";
        private const int MainThreadTimeoutMilliseconds = 20000;
        private const int MaxCallsPerFrame = 16;
        private const int MenuSettleFrames = 30;

        private static ApiServer server;
        private static string apiFilePath;
        private int menuFrames;

        internal static void Launch(string bridgeDirectory)
        {
            server = new ApiServer(ApiServer.NewToken(), MainThreadTimeoutMilliseconds);
            server.Start();
            apiFilePath = Path.Combine(bridgeDirectory, ApiFileName);
            WriteApiFile(server.Port, Process.GetCurrentProcess().Id);

            GameObject host = new GameObject("ONI AI API Host");
            DontDestroyOnLoad(host);
            host.AddComponent<ApiHost>();

            ApiLog.Event("api_started", new Dictionary<string, object>
            {
                { "port", server.Port }
            });
            UnityEngine.Debug.Log(Mod.LogPrefix + " Tool API listening on 127.0.0.1:" + server.Port);
        }

        private static void WriteApiFile(int port, int processId)
        {
            string pending = apiFilePath + ".tmp";
            File.WriteAllText(pending, new JObject
            {
                ["schema"] = "oni.api.v1",
                ["port"] = port,
                ["token"] = server.Token,
                ["pid"] = processId,
                ["mod_version"] = Mod.ModVersion
            }.ToString(Formatting.Indented));
            File.Delete(apiFilePath);
            File.Move(pending, apiFilePath);
        }

        // Something (the options screen, another mod) can turn autosave back on
        // during a session; a stuck timelapse queue freezes the simulation clock.
        private static void EnforceGuards()
        {
            int? overwrittenInterval = AutosaveGuard.Tick(Time.unscaledTime);
            if (overwrittenInterval.HasValue)
            {
                ApiLog.Event("autosave_guard_enforced", new Dictionary<string, object>
                {
                    { "overwritten_interval", overwrittenInterval.Value }
                });
            }
            TimelapseGuard.Tick(Time.unscaledTime);
        }

        private void Update()
        {
            ApiEvents.Tick(Time.unscaledTime);
            AutoloadWhenMenuIsReady();
            if (ApiLifecycle.SessionActive)
            {
                EnforceGuards();
            }

            for (int i = 0; i < MaxCallsPerFrame; i++)
            {
                ApiCall call;
                if (!server.TryTake(out call))
                {
                    return;
                }
                call.Complete(ApiTools.Invoke(call.Tool, call.Args));
            }
        }

        // Let MainMenu.OnSpawn and the frame after it finish, as the game's own
        // Resume path only runs once the menu is up.
        private void AutoloadWhenMenuIsReady()
        {
            if (string.IsNullOrEmpty(ApiMode.PendingSave) || ApiLifecycle.State() != ApiLifecycle.MainMenu)
            {
                return;
            }
            if (++menuFrames < MenuSettleFrames)
            {
                return;
            }

            string save = ApiMode.TakePendingSave();
            try
            {
                ApiLifecycle.Load(save);
            }
            catch (Exception ex)
            {
                ApiLog.Error("api_autoload", ex);
                ApiEvents.Add("autoload_failed", new Dictionary<string, object> { { "message", ex.Message } });
            }
        }

        private void OnApplicationQuit()
        {
            server.Stop();
            try
            {
                File.Delete(apiFilePath);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                // The launcher treats a dead pid in api.json as no server.
            }
        }
    }
}
