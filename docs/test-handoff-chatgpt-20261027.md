# 测试交接：由 ChatGPT 客户端执行

日期：2026-10-27 Asia/Tokyo。适用分支 `review-fixes-20260926`。

## 分工（用户决定 2026-10-27）

| 阶段 | 谁来执行 | 控制器调用的模型 |
|---|---|---|
| 开发期间的全部测试（离线测试、模型冒烟测试、M1 起的实盘轮次） | **ChatGPT 客户端** | Cockpit Tools 的本地 API 服务：只用 bitibit 账号的那一个反代服务，模型选 6.1 terra 或 6.1 sol |
| 版本完整后的最终验证 | 导入最终版本后执行 | cline-pass 订阅的 `cline-pass/deepseek-v4.1-flash` |

Claude 负责规划、写代码和离线自检，测试执行交给 ChatGPT 客户端。所有实盘规则仍以 `AGENTS.md` 为准：只用验证过的命名克隆，动手前先发检查点说明，每轮最多一次实盘测试，结束时完整收尾。

## 1. 配置控制器的模型端点

Cockpit 本地 API 已确认监听 `127.0.0.1:14861`，是 OpenAI 兼容接口，不带 key 访问 `GET /v1/models` 返回 401。端口如果变了，用 `-BaseUrl` 指定，但主机只能是本机。

1. 在 Cockpit Tools 里找到 bitibit 账号那个反代服务的 API key，以及它列出的 6.1 terra / 6.1 sol 的**确切模型 ID**。
2. 运行配置脚本（key 会被隐藏输入，并只写入用户环境变量 `COCKPIT_API_KEY`）：

```powershell
powershell -File scripts/configure-oni-model.ps1 -Provider cockpit -Model <Cockpit 列出的模型 ID>
```

3. 检查配置（不会发出请求）：

```powershell
powershell -File scripts/configure-oni-model.ps1 -Provider cockpit -Show
```

`cockpit` 预设的安全规则：

- 没有默认模型，不给 `-Model` 会直接报错。
- 只能发往 `127.0.0.1`、`localhost` 或 `::1`。端点指向其他主机时，配置报 `provider_endpoint_mismatch`，key 不会被发送。
- 默认用 `chat_completions` 协议。如果确认反代支持 `/responses`，可以加 `-WireApi responses`。

## 2. 离线测试

每个模块单独运行（一次全部运行会因为某个模块在导入时找不到文件而整体中断）：

```bash
for f in scripts/test_*.py; do py -3 -m unittest scripts.$(basename $f .py) 2>&1 | tail -1 | sed "s|^|$f: |"; done
```

截至 `1b62a85` 之后的这一轮：74 个模块中只有 5 个不是 OK，都是已知的（路线图 B17）。4 个 bootstrap 模块需要实盘引导生成的报告；`test_transaction_tree_long_path` 是一个没有测试用例的脚本。**出现第 6 个就是回归。**

不要在跑测试的同时执行 `dotnet build`：`test_executor_model_command_contracts` 会调用 C# harness，并行构建会争用产物导致误报。

## 3. 模型冒烟测试（不碰游戏）

```powershell
py -3 -B scripts/verify_oni_model_integration.py
```

它用回归测试的 bridge 夹具，让真实的 model loop 发出一次真实的选择请求。报告里要核对这几项：

- `config.provider` 应为 `cockpit`，`config.base_url_host` 应为 `127.0.0.1`。
- `config.config_error` 应为空。
- `structured_output` 是实际用上的模式（`json_schema` 或 `plain`）。
- 模型选中的 `candidate_id`。

如果反代不支持 `json_schema`，正常情况是先收到 400，然后自动降级到 `plain`。

## 4. 兼容的数据格式

`scripts/test_model_format_compat.py` 固定了下面这些格式，换模型时以它为准：

| 来源 | 读取的形状 |
|---|---|
| OpenAI Responses API | `output` 里先是 `reasoning` 条目、再是 `message`；`refusal` 块；`status: incomplete`；`*_tokens_details` 里的推理和缓存 token |
| OpenAI Chat Completions | `message.refusal`；`finish_reason` 为 `length` 或 `content_filter` |
| DeepSeek | `reasoning_content` 永远不会被当作答案；`prompt_cache_hit_tokens`；JSON 模式只用 `json_object` |
| 网关包装 | cline-pass 的 `{"data": {...}, "success": true}`；HTTP 200 正文里带的错误 |
| 正文习惯 | 内联的 `<think>…</think>`、前后带说明文字、代码块 |

失败时的状态码：`model_refused`、`response_incomplete`、`content_filtered`、`invalid_json`、`invalid_output_schema`、`invalid_candidate_reference`、`http_4xx` 等。用量记录在 `usage`（输入、输出、总计）和 `usage_details`（推理 token、缓存命中 token）里。

## 5. 实盘轮次

按 `docs/roadmap-20261027.md` 的 M1 执行，前提条件：

- Terra 源存档（`m1-terra-src`）已经建好，并按命名克隆流程准入。
- 启动控制器用 `scripts/start-controller.ps1`，观察用 `scripts/watch_controller.py`，两者放在不同的命令里。
- 每轮结束后按 AGENTS.md 收尾，并核对存档基线（`scripts/save_tree_baseline.py compare`）。
- 进展要跨进程保留：准备时加 `--checkpoints`（配置里会写入 `checkpoints: true`），游戏的加载指针用 `scripts/set_oni_load_save.py --latest-checkpoint <克隆清单> <克隆 .sav>` 设置。停止时控制器会自动存出下一环检查点，状态文档里的 `checkpoint` 字段会给出结果（`verified` 或 `failed` 及原因）。第一次带检查点的实盘测试要专门核对 `docs/b16-checkpoint-design-20261027.md` 末尾列出的待确认风险。

## 6. 最终验证切回 DeepSeek

```powershell
powershell -File scripts/configure-oni-model.ps1 -Provider cline-pass
```

然后重跑第 3 节的冒烟测试，`config.provider` 应为 `cline-pass`、`base_url_host` 应为 `api.cline.bot`，之后再按同样的实盘流程做最终验证。
