# ONI 内置 Agent v1.2：首版源码交付状态

日期：2026-11-07。基线：`783f83e6ab45e87d6d46d70d00e4c47b3b5dbfd0`。

**这是受保护测试克隆上的开发基础版本，不是 v1.2 最终产品已完成，也不是已构建或已实机验证的 Mod。**

最终需求原文保留在 [inmod-agent-v1.2-plan.md](inmod-agent-v1.2-plan.md)。源码工具矩阵见 [inmod-agent-tool-contracts.md](inmod-agent-tool-contracts.md)。

## 已写入的实现

- OpenAI Chat Completions 非流式异步客户端：明确 Base URL/Model，HTTPS 默认，显式本地 HTTP，拒绝重定向，限制请求、响应、解析深度、超时、重试和 429 退避。连接测试只使用本地 echo 工具闭环，不触发游戏工具。
- 独立 AgentConfig 与 Windows 当前用户 DPAPI 凭据存储。普通配置不含 Key，不支持加密的平台拒绝保存，没有明文退路。
- 单 worker AgentRuntime：开始、暂停、继续、停止、纠偏；单调 generation 和 session；取消等待和旧响应；多工具串行；纠偏后重新观察；轮次、调用次数和任务时限预算。完整工具调用/结果组裁剪，超大结果明确标记截断。
- 直接主线程 GameToolDispatcher，不通过 localhost 自调用。派发前核对运行状态、session、generation；队列有界。内置 wait 使用可取消异步等待和 get_events，保留紧急事件提前结束语义。
- 共享 schema 的 C# 参数校验，在 handler 前拒绝未知参数、缺项、类型、枚举和范围问题。schema 直接作为资源嵌入，不维护第二份工具清单。工具数量动态统计。
- 内置 Agent 获取会话写权限期间，外部 HTTP/MCP 写入拒绝竞争，load_save 也纳入写权限判断。内置 Agent 的路径式 load_save 永远拒绝，不能充当 L3。
- Alt+F12 开发控制台：配置、连接测试、公开摘要、工具参数/结果的脱敏有界预览、对话纠偏、开始/暂停/继续/停止。异步轮转审计；不把内置调用复制进旧的同步原始审计文件。
- Agent 暂停和游戏暂停分开。Pause/Unpause hooks 不全时拒绝 Agent 变速；玩家或游戏对话框持有的暂停不可被 Agent 恢复。
- 从现有玩法资料嵌入有界的开局/生存知识包，在每次创建 runtime 时按当前周期选择。不是向量库，也没有重新引入旧规则控制器。
- 独立 RollbackBudgetLedger 核心及测试：当前 session/colony/opaque autosave 验证接口、原子 pending 扣减、失败不返还、重启保留、损坏/缺失/回放 fail-closed。**尚未连接真实游戏回档。**

## 明确尚未完成

1. 新游戏候选读取、特质刷新、初始阵容选择和确认开局。仓库中只有打印舱相关 API，不能把它当成新游戏接口；没有目标 ONI 程序集可核对具体签名。
2. Mod Manager 的原生设置入口和正常新游戏启动集成。目前控制台宿主仅在现有、显式 api_session 测试克隆流程中运行，不在普通启动时接管游戏。
3. 原生 autosave 的安全枚举、当前 clone/colony 身份证明、轮换目标检查、实际加载与回档后重绑。L3 保持关闭，原 AutosaveGuard 不变。
4. `allow_pause_require_resume` 的机器可验证恢复条件。该策略目前拒绝 Agent 发起新暂停；可用策略是 `agent_decides` 与 `never_pause`。快捷键当前固定 Alt+F12，重映射尚未接入。
5. 配置中的回档上限目前仅为未来集成设置；不会启用回档。没有假冒自动存档频率控制。
6. 成就专用验证器、全工具实机冒烟、长时间内存/GC/帧时间测量、真实模型兼容矩阵、完整新游戏到殖民地流程验收。
7. 不保证工具操作事务性。现有 `configure` 中一些依赖游戏对象的检查仍可能在前面的设置生效后失败；通用 schema 校验仅提前拦截结构性错误。

## 为什么保留 AutosaveGuard

现有源码记录了真实的存档风险：字节复制的测试克隆保留原殖民地 GUID；来自 autosave 的加载还可能将活动存档路径指回 originalSaveName。ONI 原生轮换因此可能删除原殖民地 autosave。没有当前游戏版本的实测证据前解除保护是不安全的。

这里没有建立新的 autosave 镜像系统，也没有绕开 v1.2 方案。下一步必须在测试环境核实当前 clone 的原生 autosave 目标，随后按最终方案调整保护而不是直接删除保护。

## 验证记录

在本次 dot 工作环境执行：

- `python3 -m unittest discover -s scripts -p 'test_*.py' -v`：98 项，95 通过，3 跳过。
- 新增 5 项 Python 源码契约检查通过。它们检查打包、schema 注册、保护边界和关键源码约束，**不是 C# 行为测试**。
- `python3 scripts/check_canonical.py`：通过，33 个 C# handlers，另有 wait 适配。
- `git diff --check`：通过。
- 没有 .NET SDK、C# 编译器、PowerShell 或 ONI managed assemblies。**Mod 编译、C# harness 和游戏实测均未运行**，不算通过。
- 未安装依赖，未读取/写入真实游戏存档，未保存真实 API Key，未请求真实模型服务，未启动游戏。

新增 `tools/AgentRuntimeHarness` 直接链接生产 C# 核心，覆盖多工具串行、取消/迟到响应、纠偏、预算、成组历史、schema、写权限、mock HTTP、凭据保护边界和回档账本。它不依赖 Unity 或实际游戏，但需要 .NET 8 与合法可用的 Newtonsoft.Json.dll。

在具备依赖的开发机运行：

```powershell
dotnet run --project tools/AgentRuntimeHarness -c Release -p:NewtonsoftJsonPath="<path>\Newtonsoft.Json.dll"
powershell -File scripts/build-mod.ps1 -ONIManagedDir "<game>\OxygenNotIncluded_Data\Managed"
python -m unittest discover -s scripts -p 'test_*.py' -v
```

不得把 harness 通过等同于游戏集成通过。还需验证 Unity 输入/窗口焦点、Harmony 生命周期、主线程执行与所有 handler。

## 构建后在测试克隆上尝试的顺序

1. 先完成上述编译和 harness。再按 README 原有命名测试克隆流程构建、安装和启动；不得加载原始存档。
2. 不同时运行外部玩家或 `watch_player.py --no-pause`。外部读取可以保留。
3. Alt+F12 打开控制台 Settings。填写明确的 Base URL、Model 和 Key；界面显示最终请求地址。配置里的 Enabled 默认关闭。
4. Test connection 会将无副作用 echo 请求发往所选服务。Save credential 才会保存 DPAPI 凭据；Save settings 不包含 Key。
5. 开启开发 Agent，确认当前测试克隆，再按 Start。不会仅因曾保存 Enabled 就自动开始。Start 后所选服务会收到任务、知识、游戏观察与工具结果。
6. 配置更改先停止并退役旧 runtime。Pause Agent、Stop 不改变游戏速度；若游戏已经由玩家/加载界面暂停，需要玩家自行恢复。
7. 任务达到预算或模型无工具调用时暂停，查看日志再决定继续/新任务。不要把模型文字宣称的成就当作已验证完成。

## 继续完成 v1.2 所需输入

- 获得可用于构建/检查的对应版本 ONI managed assemblies 和 .NET 环境。
- 在用户授权的游戏测试环境核实新游戏内部 controller 与生命周期。
- 证明原生 autosave 路径、GUID、轮换和加载都限制在当前受控测试殖民地，再集成 ledger，保留源存档保护。
- 完成最终方案 T01–T26 的实际证据表，再将状态从开发基础版本升级为发布候选。
