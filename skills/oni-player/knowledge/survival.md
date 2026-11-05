# 生存数字

下面的数字查自当前游戏版本（构建 744825）的代码，标“约”的是经验值。实际以 `get_buildable` 和 `inspect` 返回的为准。

## 优先级

氧气 > 食物 > 卫生 > 电力 > 温度。上游没解决时，别在下游花时间。

## 一个复制人的消耗

| 项 | 数值 |
|---|---|
| 氧气 | 0.1 kg/s，每周期 60 kg |
| 卡路里 | 每周期 1000 kcal |

一个周期 600 秒（1 倍速下的真实时间），最后约八分之一是夜晚。

## 氧气

| 建筑 | 输入 | 产出 | 备注 |
|---|---|---|---|
| `MineralDeoxidizer` | 藻类 0.55 kg/s，120 W | 氧气 0.5 kg/s（30°C） | 开局已解锁，够 5 人用。要电，复制人要把藻类运进去 |
| `AlgaeHabitat` | 藻类 0.03 kg/s，水 0.3 kg/s，要光 | 氧气 0.04 kg/s | 不用电但产量低，一台只够约半个人，会产污水。需要 `FarmingTech` |
| `Electrolyzer` | 水 1 kg/s，120 W | 氧气 0.888 kg/s，氢气 0.112 kg/s（70°C） | 需要 `ImprovedOxygen`，而它要先研究 `LiquidPiping`。要接液管，还要处理氢气和高温 |

开局就有的氧气是有限的。`get_status` 的 `colony.breathable_kg` 和 `oxygen_report_kg` 能看出趋势。

## 食物

| 来源 | 数据 | 备注 |
|---|---|---|
| 野生 `BasicSingleHarvestPlant` | 约 3 个周期成熟（1800 秒） | 地图上已有。用 `find(plant)` 找，`harvest` 标记收割。不用研究 |
| `MicrobeMusher` 的 `MushBar` | 每份 75 kg 土 + 75 kg 水，40 秒，240 W | 要选配方，水要有来源。吃了可能食物中毒 |
| `PlanterBox` | 需要 `FarmingTech` 和种子 | 收割野生植物或挖掘时可能得到种子 |

开局 `get_status` 的 `food_kcal` 可能是 0，但复制人身上还有约 3500 kcal，够撑两三天。要在这之前有稳定的食物来源。

`food_days` 只算库存，地里的庄稼不算。

## 卫生

- `Outhouse`：每次用 13 kg 土，15 次要清空一回，复制人会自己清。产物是污染土，要注意堆放。
- 没厕所时复制人会尿在地上，污染加压力。
- `WashBasin` 放在厕所旁边，减少病菌传播。

## 电力

| 建筑 | 数据 | 备注 |
|---|---|---|
| `ManualGenerator` | 400 W | 要一个复制人一直摇，占人手 |
| `Battery` | 10 kJ | 用来缓冲，让发电机不必一直转 |
| `Generator` | 需要 `Combustion` | 烧煤，不占人手 |
| `Wire` | 约 1000 W 上限 | 超载会损坏 |

## 研究

- `ResearchCenter`：400 kg 金属，60 W，每个研究点消耗 50 kg 土，要有人操作。
- 没有研究站时，`set_research` 能设置目标，但不会有进度。
- 金属不够时先挖铜矿（`Cuprite`）。`find(element, "Cuprite")` 找位置。

## 床

每人一张 `Bed`，否则睡地上，压力上升、恢复变差。开局值得尽早建。

## 士气

`get_status` 的 `morale` 低于 `morale_expected` 时压力会涨。每学一项技能期望就升一点，所以别一口气花完技能点，先备好 `WaterCooler`、`DiningTable`、更好的食物和装饰。
