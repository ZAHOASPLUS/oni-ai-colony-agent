# ONI AI Colony Agent

Let an external LLM play *Oxygen Not Included* in real time.

A game Mod exposes a small HTTP tool API on the loopback interface; a Python
layer turns that API into tools (an MCP server, or OpenAI-style function
calling) that any model can call to observe the colony, dig, build, research,
assign skills and save checkpoints.

> Unofficial fan project. Not affiliated with or endorsed by Klei
> Entertainment. You need your own copy of *Oxygen Not Included*; no game code
> or assets are included in this repository.

中文说明：这是一个让外部大模型实时玩《缺氧》的工具链。游戏内 Mod 在本机回环地址上提供工具 API，
`tools/oni_tools.py` 把它包装成 MCP 服务或函数调用工具。项目内部的工作规则和设计记录主要是中文，
见 `AGENTS.md` 和 `docs/`。

## Layout

| Path | What |
|---|---|
| `mod/` | The game Mod (C#, Harmony, `netstandard2.1`). `mod/Source/Api/` is the tool API. |
| `tools/oni-tools.json` | The single tool definition shared by every player. |
| `tools/oni_tools.py` | `mcp` (stdio MCP server) and `call <tool> '<json>'` (one call from the shell). Standard library only. |
| `tools/antigravity_player.py`, `agent/player_loop.py` | Player loops for other model front ends. |
| `scripts/api_session.py` | Start / stop a game session on a named test clone. |
| `scripts/watch_player.py` | Supervisor watcher (audit log, no-pause enforcement). |
| `skills/oni-player/` | Play skill and gameplay knowledge for the model. |
| `docs/` | Design notes and results of live runs. |

## Requirements

- Windows, *Oxygen Not Included* (Steam).
- .NET SDK (to build the Mod against the game's managed assemblies).
- Python 3.10+ (standard library only).

## Build and install the Mod

```powershell
# Builds and installs in one step. -ONIManagedDir defaults to the standard
# Steam location (C:\Program Files (x86)\Steam\...); pass it if the game is elsewhere.
powershell -File scripts/install-dev-mod.ps1 -ONIManagedDir "<game>\OxygenNotIncluded_Data\Managed"
```

(`scripts/build-mod.ps1` takes the same `-ONIManagedDir` if you only want to build.)

This installs to `Documents\Klei\OxygenNotIncluded\mods\Dev\oni-ai-colony-agent`.
Enable it in the in-game Mods menu.

## Run a session

The API is off unless a session asks for it, and the tools only ever play a
**named test clone**, never your original save:

```powershell
powershell -File scripts/create-named-test-clone.ps1 -SourceSave <your .sav> -TargetStem oni-my-clone -RunId my-run   # writes test-clone-manifest.json; the name must start with oni-
python scripts/api_session.py start --manifest <manifest> --save <clone .sav> --run-dir reports/api/<run>
python tools/oni_tools.py call get_status '{}'
python scripts/api_session.py stop --run-dir reports/api/<run>
```

`stop` restores game preferences and checks that the original save is
unchanged. The Mod writes `api.json` (port + token) to
`Documents\Klei\OxygenNotIncluded\ai-bridge`. To move that folder, set
`ONI_AI_BRIDGE_DIR` as a user-level environment variable and restart Steam:
the game is launched through Steam, so a variable set only in your shell
reaches the scripts but not the Mod.

To use the tools from an MCP client, point it at `python tools/oni_tools.py mcp`
(see `.mcp.json`). For Antigravity, `python tools/antigravity_player.py
mcp-config` prints a config with absolute paths for your machine.

The DeepSeek player (`agent/`) reads its key from the environment; see
`.env.example`. It refuses to send a key unless you also choose a provider
(`ONI_AI_PROVIDER`) or an endpoint (`OPENAI_BASE_URL`), so a generic
`OPENAI_API_KEY` is never sent to a gateway you did not pick. Never commit a
real key.

## Tests

```powershell
python scripts/test_oni_tools.py   # and the other scripts/test_*.py
```

## License

MIT, see `LICENSE`.
