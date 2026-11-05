# Antigravity 工具接口（2026-11-04）

在统一的 `tools/oni-tools.json` 与 `tools/oni_tools.py` 边界之上，新增 Google Antigravity 接入层，使 Antigravity IDE / App / CLI 与 Gemini `generateContent` 模型可以无键鼠直接操作缺氧（ONI）命名测试克隆存档。

## 组成

1. **工具声明导出（`tools/oni_tools.py::antigravity_tools`）**
   - 直接从唯一来源 `tools/oni-tools.json` 生成 Gemini / Antigravity `functionDeclarations`，自动剥离 `additionalProperties` 元字段，不在适配器中维护第二份 schema。
2. **Antigravity 玩家与会话桥（`tools/antigravity_player.py`）**
   - `AntigravitySession`（交互式代理直连模式：`status`、`step`、`wait`、`call`）：
     - 在 `--run-dir reports/play/<run>` 下自动持久化事件游标 `cursor.json` 与完整原始调用审计 `steps.jsonl`；
     - 单次 `wait` 默认按 `AGENTS.md` 上限封顶为 `<= 30` 秒，并在等待后自动附带 `get_status` 检查；
     - `compact_observation` 自动折叠批量电线/管道/地砖/梯子竣工刷屏，保留告警、复制人生存指标、建筑状态与在排配方。
   - `AntigravityPlayer`（独立模型循环：`run`）：
     - 无第三方依赖实现 Gemini / Antigravity `generateContent` 多轮工具调用循环（保留 `thought` / `thoughtSignature` 片段，串行执行 `functionCall` 并回传 `functionResponse`）。
3. **Antigravity 工作区定制（`.agents/` 与 `skills/oni-player/SKILL.md`）**
   - `skills/oni-player/SKILL.md` 补齐标准 YAML frontmatter，并通过 `.agents/skills.json` 注册到工作区；
   - `.agents/plugins/oni-player/`（`plugin.json` + `mcp_config.json`）及 `.agents/plugins.json` 打包 `oni` stdio MCP 服务（调用 `tools/oni_tools.py mcp`）。

## 常用命令

```powershell
# 读取自上次游标以来的事件与殖民地紧凑状态
python -B tools/antigravity_player.py status --run-dir reports/play/agy-r01

# 批量执行显式动作并记录审计日志
python -B tools/antigravity_player.py step --run-dir reports/play/agy-r01 '[["set_speed",{"speed":0}],["inspect",{"x":100,"y":223}]]'

# 推进最多 25 秒（遇紧急事件提前返回）并读取最新状态
python -B tools/antigravity_player.py wait 25 --run-dir reports/play/agy-r01

# 输出 Antigravity MCP 配置片段
python -B tools/antigravity_player.py mcp-config
```

## 进程与启动注意事项（Windows / Antigravity）

Antigravity 的 `run_command` 运行在受限终端沙箱中：切勿在 `run_command` 内直接启动 `steam.exe`（沙箱会限制 `steamwebhelper` 的 GPU 与桌面窗口并占用全局 Steam 单例锁 `ActiveProcess`）。正确方式是**先在宿主机桌面正常打开并登录 Steam 客户端**，随后在命令中运行 `python -B scripts/api_session.py start ...`——`steam://rungameid/457140` 会通过 IPC 交由桌面 Steam 在沙箱外使用独立显卡拉起 `OxygenNotIncluded.exe`，且不会随短命令返回而被回收。

## 离线校验

```powershell
python -B scripts/test_oni_tools.py
python -B scripts/test_antigravity_player.py
python -B -m py_compile tools/oni_tools.py tools/antigravity_player.py
```
