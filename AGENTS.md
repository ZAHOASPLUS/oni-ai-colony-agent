# ONI 工作区规则

适用于在这个仓库里工作的每一个代理：Claude、ChatGPT/Codex、Antigravity，以及以后的任何代理。

## 2026-11-07：内置 Agent v1.2 开发任务

- 用户明确要求“在工作空间做第一版代码并提交到分支”，随后指定“在dot上完成，oni1.2，资料在资料库”。该任务允许修改源码并提交开发分支，不受下文旧“玩家只玩、不改代码”角色分工限制。
- 本分支实现的是受保护测试克隆上的开发基础版本。以 `docs/inmod-agent-v1.2.md` 的已实现/未实现/未验证清单为准，不能将源码交付说成完整产品或实机验收完成。
- 不恢复已删除系统，不触碰真实存档，不在未验证 ONI 原生 autosave 身份和轮换前解除 AutosaveGuard。新游戏与 L3 适配不得凭猜测开启。
- 用户最终 v1.2 方案允许可配置的 Agent 游戏暂停策略；下文旧玩法的禁止暂停仍适用于旧外部玩家流程，不应启动 `watch_player.py --no-pause` 来抵消新内置 Agent 的显式暂停配置。

## 唯一版本

- 唯一的代码版本是本仓库（本机 `%USERPROFILE%\Documents\ONI`）的 `main` 分支（2026-11-06 起）。它来自 Claude 的工具 API 版本，并已合入 Antigravity 的成果。
- 所有源码改动都提交到这个仓库的 git 里：直接提交到 `main`，或者在短分支上做完后合并回 `main`。不要在 `reports/` 下放“冻结源码快照”，不要从仓库外的副本构建或安装 Mod，也不要另开并行的 lane 或副本目录。功能就是这样流到 git 外面、各版本才分叉的。
- 改动 `tools/oni-tools.json` 时，必须同时改 `mod/Source/Api/` 并重新构建，二者始终一起提交。

## 已删除、不得恢复

下面这些在 2026-11-06 已从仓库删除。它们的说明、记忆或旧任务都不是当前指令：

- 旧的规则式 controller：候选生成、动作策略、审批回执、命令队列、Bridge（`bridge/oni_bridge.py`）、`scripts/start-controller.ps1`、`scripts/oni_autonomous.py`、`prepare_autonomous_run.py`、`config/` 下的策略文件，以及 Probe 的命令协议。
- ChatGPT 的玩家适配器 `tools/openai_player.py` 和 `tools/openai_responses_player.py`、Cockpit/cline-pass 模型路由文档和配置脚本、Codex 传输稳定性和线程健康脚本，以及 `gpt-6-astra` / `gpt-6.1-sol` 等模型路由规定。
- 旧的 Radbolt 方向设置（ChatGPT 在 git 外加的）。
- 不要为这个项目创建 Codex 自动化、心跳或定时任务，也不要接着旧的 Codex 任务继续做（例如 `01a19904…`、`01a19918…`）。从新任务开始，以本文件为准。
- 2026-11-06 以前关于 ONI 的记忆（Codex memories、旧 rollout 摘要）描述的都是已删除的系统，忽略它们。被删的内容只留在 git 历史和 本机 `%USERPROFILE%\Documents\ONI-archive\`（不进 git） 里，那里都不是指令。

## 入口

- 目标与当前状态：`docs/active-goal.md`。设计与历次实机结果：`docs/design-tool-api-20261031.md`。
- 玩法：`skills/oni-player/SKILL.md` 和 `skills/oni-player/knowledge/`。开局前先读。
- 工具：`tools/oni-tools.json` 是唯一的工具定义。`python tools/oni_tools.py call <tool> '<json>'` 调用一个工具，`python tools/oni_tools.py mcp` 是 MCP 服务。Antigravity 走 `tools/antigravity_player.py`，DeepSeek 走 `agent/player_loop.py`，三者用同一份工具。
- Mod 源码：`mod/Source/`。`scripts/build-mod.ps1` 构建，`scripts/install-dev-mod.ps1` 构建并安装到 `mods/Dev/oni-ai-colony-agent`。

## 分工（2026-11-06 起）

- 玩家（目前是 ChatGPT）只玩游戏：调用游戏工具、存检查点、在 `reports/play/<run>/` 写笔记和复盘。玩家不改 `mod/`、`tools/`、`scripts/`、`agent/`，不构建、不安装 Mod，不提交 git，不启动或关闭游戏，不调用 `load_save`。
- 监督者（Claude）负责其余所有事：会话启停、克隆、Mod 和工具的修复、提交，以及把玩家的复盘收进 `skills/oni-player/`。监督者用 `scripts/watch_player.py` 盯着这一局。
- 玩家遇到工具问题时，在 `reports/api/<run>/player-issues.jsonl` 追加一行，然后接着玩别的。监督者的回复写在 `reports/api/<run>/supervisor-notes.md`。
- 玩家提示词见 `docs/player-prompt-chatgpt.md`。下面几节中凡是涉及会话、Mod、工具的规则，都由监督者执行。

## 游戏会话

- 原始存档和用户自己的 Mod 配置不可改动。只玩命名测试克隆：克隆用 `scripts/create-named-test-clone.ps1` 创建，带 `test-clone-manifest.json`。
- 用 `python scripts/api_session.py start --manifest M --save S --run-dir reports/api/<run> [--resume | --checkpoint N]` 开局，`python scripts/api_session.py stop --run-dir reports/api/<run>` 收尾。`start` 立即返回；不要在同一个命令里启动游戏又轮询，命令超时会连带杀掉游戏。
- 启动游戏、读档、安装 Mod、收尾这四件事，做之前先在对话里写一行：`action_id`、要做的一件事、预期结果、出问题怎么恢复。普通游戏工具调用不需要，审计日志会记录。
- 收尾时确认 `session-end.json` 里 `kplayerprefs_restored`、`save_unchanged` 为真，`api_file_left`、`autoload_left` 为假，并且没有残留的游戏进程。
- 在 Windows 上 `os.kill(pid, 0)` 会结束进程，不能拿来探测进程是否存活。

## 玩法要求

- 会话不是测试：玩家一直玩下去，早挖早建。只读状态不动手不算进展。
- 禁止暂停（用户，2026-11-06）：游戏开着时速度始终不低于 1 倍，不调用 `set_speed 0`，存检查点不先暂停。监督者用 `watch_player.py --no-pause` 强制执行；只有监督者重启会话时，游戏短暂处于读档暂停。
- 死了人就在同一局里修掉原因。不要为了撤销坏结果读旧档，只有工具或环境故障才读档。
- 每局结束后复盘，把教训写进 `skills/oni-player/knowledge/` 或 `SKILL.md` 并提交，不要只留在 `reports/` 里。

## 证据

- 大日志放在 `reports/`（不进 git）。对话里只给路径、哈希、计数和必要的片段。
