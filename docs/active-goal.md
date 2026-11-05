# ONI 当前目标

更新：2026-11-06。规则见 `AGENTS.md`，设计和历次实机结果见 `docs/design-tool-api-20261031.md`。

## 终极目标（用户，2026-10-31 起）

1. AI 通过程序接口玩缺氧：读状态、下操作都走工具，全程不用鼠标键盘。
2. AI 主动地玩：自己观察、计划、执行、复盘，没有规则替它出选项或等触发。
3. 一局一局玩下去，每局结束复盘，不断迭代，直到达成**永生档**或某个**终极成就**。永生档指氧气、水、食物和资源都形成永久或半永久的循环。

阶段：S1 是连续 50 个周期稳定存活（第四局 `oni-s1-r04` 已达成），S2 是完成一个官方使命，目前选的是 Thriving（安居乐业）。

## 当前状态（2026-11-06）

代码已统一到 `main`。2026-11-06 首次续玩已实机确认复制人 id 操作、蘑菇条每份增加 800 kcal 的 cooked 计数，以及完整周期的 yesterday_cooked；电力字段为 kJ。已发现并修复旧审计混入封链的问题。按摩床分配与压力阈值配置正在本轮补齐、构建和验证。

| 殖民地 | 玩家 | 最新检查点 | 情况 |
|---|---|---|---|
| `oni-agy-r23` | Codex 续玩 | `oni-agy-r23-ckpt-002`，周期 199.101 | 7 人，本轮零减员；芙蕾娅已吃饭，收尾 1171 kcal、压力 85.1%。第 198 周期食物净值 0 kcal、氧气 −130 kg、电力 +42 kJ，库存仍为 0，禁止加人。最新记录 `reports/play/r23-codex-20261106-a/notes.md`、`review.md`。 |
| `oni-s1-r06` | Claude | 周期 224 | 5 人 0 减员。遗留问题见设计文档 P7。 |
| `oni-s1-r05` | Claude | ckpt-076，周期 256 | 能活但全在救火，不建议继续。 |

## 下一步

1. 从 `oni-agy-r23` 的 ckpt-002 继续。开局前先读 `skills/oni-player/SKILL.md`（尤其是“殖民地规则”）和 `reports/play/r23-codex-20261106-a/notes.md`。校验哈希 `1f9549dfce8a91f4ae1d13c0af105f3006984728be2174e03f8e74875abc2e89`。
2. 先让食物、氧气、电力都转为盈余，再按使命要求加人。
3. 每局结束后在 `reports/play/<run>/review.md` 写复盘，把通用的教训提交到 `skills/oni-player/`。
4. 工具不够用时，在 `mod/Source/Api/` 和 `tools/oni-tools.json` 里一起补，构建并提交后再上实机。

本次优先事项：给按摩床 `(123,213)` id100422 设置芙蕾娅专用及合适阈值；给除臭器送过滤介质，保持煤机供料；完成 `(99,198)`、`(105,198)` 冰质变温板并验收作物恢复。玻璃熔炉和破碎机的非必要配方已清零。不要把停电导致的低用电当成供电充裕。

续玩 `oni-agy-r23`（ckpt-002 已封进链，`--resume` 会读它）：

```
python scripts/api_session.py start --manifest reports/runtime/agy-r23/test-clone-manifest.json --save C:\Users\user\Documents\Klei\OxygenNotIncluded\cloud_save_files\76561190000000000\oni-agy-r23\oni-agy-r23.sav --run-dir reports/api/<新的 run 名> --resume
```

开新克隆用 `scripts/create-named-test-clone.ps1`。从检查点续玩用 `scripts/api_session.py start --resume`：它读取克隆检查点链里最新的一环，所以检查点要先由 `api_session.py stop` 或 `seal` 封进链里。
