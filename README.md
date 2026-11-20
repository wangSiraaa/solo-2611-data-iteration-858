# obsdelivery — 观测语义服务与十进制交付库

纯 Python 3.11 + 标准库（`sqlite3`、`fractions.Fraction`）。不依赖宿主浮点：
二进制位型解释、参考核对、系数取整、误差判定全部用精确有理数完成。

```
obsdelivery/
  semantics.py      位型解释 + 参考判定 + 热循环闭合（纯函数，无存储）
  service.py        语义服务：观测接纳、参考版本、报告、规范观测产品
  delivery_algo.py  十进制表示选择（纯函数，无存储）
  contracts.py      独立读者合同校验与身份
  delivery.py       交付任务：产品 × 样品集 × 合同，全有或全无
  thermal.py        热循环产品：设备目录 × 冻结读数 × 闭合事件
  physical.py       实物转盘回放：工位日志 × 转盘配置 × 化验单核对
  public.py         公开产品/交付/热产品/核对文档 + 独立读回校验
  storage.py        SQLite schema（不可变表 + 显式事务）
  demo.py           跑 materials/739 + J0/J1
  thermal_demo.py   热循环与留样追溯的组合演示
tests/              精确断言、失败原子性、规模测试
```

运行：

```
python3 -m obsdelivery.demo          # 人读输出
python3 -m obsdelivery.demo --json   # 结构化输出
python3 -m obsdelivery.demo --db /tmp/obs.sqlite
python3 -m obsdelivery.thermal_demo  # 热循环 + 留样转盘 + 重开复核
python3 -m pytest -q                  # 需 pytest；用 venv 安装即可
```

---

## 1. 位型语义

32 位格式：bit31 sign，bits30..23 exponent，bits22..0 fraction。

| 条件 | 精确值 |
|---|---|
| exp=0, frac=0 | 带符号零（`sign` 原样保留），值 0 |
| exp=0, frac>0 | `(-1)^sign · frac · 2^-149` |
| 1..254 | `(-1)^sign · (2^23+frac) · 2^(exp-150)` |
| 255, frac=0 | 带符号无穷 |
| 255, frac>0 | NaN，payload = fraction 原值 |

`interpret(bits)` 返回 `kind`（`FINITE/ZERO/INFINITY/NAN`）、`bits`、`sign`、
NaN `payload`，有限时返回精确 `Fraction` 值。零的符号与原件身份由 `bits`
与 `sign` 共同保留；负零的交付证据里明确注明“整数系数无法携带 −0，
身份由 original_bits/sign 保留”。

参考判定只看**原值**：

* 有限且 `|value − expected| ≤ tolerance` → `PASS`，否则 `OUTSIDE`；
* 非有限 → `NONFINITE`；无观测 → `MISSING`；
* 报告保存 `abs_difference` 精确差，永不使用交付舍入值。

## 2. 观测接纳

* 按 `(sample_id, revision)` 追加，最高 revision 生效；
* 同版同 bits → 重放（不写新状态）；同版异 bits → **整批拒绝**（`BatchRejected`），
  批次与全部成员以 `rejected` 记入审计，不触碰任何观测；
* 低 revision 合法 → 接纳但 `ignored_older`，不覆盖高版；
* 拒绝是按整个请求批次原子的，`s9` 等未被冲突的记录也不会写入。

## 3. 语义产品与报告

`report()` 把当时的参考版本 + 生效观测固化为不可变行（内容摘要自校验）。
`publish_semantic_product()` 生成独立于交付的规范观测产品，可由服务单独查询。
旧报告/旧产品在新观测、新参考出现后仍按原内容读回，摘要一致；同 id 异内容
直接冲突。参考的派生版本记录 `parent_version`，“复制旧完整版本、替换所列项、
保留未列项”可复现。

## 4. 交付：独立读者合同

合同字段：非空 `allowed_scales ⊆ {0..9}`、正 `max_abs_coefficient`、
非负有理 `max_abs_error`。读者值 = `coefficient / 10^scale`。

对每个允许 scale `s`：

```
coef  = nearest_integer(value · 10^s)      # 精确运算
tie   = 恰好 .5 时取偶系数                 # round_half_even
合法  ⇔  |coef| ≤ max_abs_coefficient  且  |coef/10^s − value| ≤ max_abs_error
```

**scale 选择策略**：按合同给定的 `allowed_scales` 顺序（通常升序）逐个精确
尝试，取第一个合法者。合法的较小 scale 给出更短系数；同一值在多个 scale
都合法时，先出现者胜出。这是有意的文档化策略，不宣称最优体积/精度。
关键点是：**每个**允许 scale 都被精确评估过，绝不会因为只试一个 scale 就拒绝。

**误差证据**：每行保存精确 `error = coefficient/10^scale − original`（带符号）、
`|error|`、全部 scale 的尝试记录（系数、误差、两个界是否满足）以及所选用
系数/scale。`public.verify_public_delivery` 无需数据库即可重算系数界、误差界、
reader 值与状态一致性。

**非有限不能伪造**：`NONFINITE`（NaN/±Inf）与 `MISSING` 没有有限十进制表示，
任一被请求记录无合法表示 → 整个任务拒绝，`deliveries`/`delivery_line` 零写入。
失败只在 `delivery_attempts` 留审计行，不占用成功 `delivery_id`；同一 id 在失败后
可用更合适的合同成功提交。

**OUTSIDE 不会被洗白**：交付行的 `delivered_status` 始终等于原语义 `status`，
即使所交付的十进制恰好等于 `expected`（例如 0.1f 交付成 `100/10^3` 仍是
`OUTSIDE`）。读回校验会检查 `status == delivered_status`。

**身份与重放**：成功 `delivery_id` 绑定 `(产品, 样品集合, 合同)`；同请求重放
返回原内容摘要（`created=False`），异请求抛 `DeliveryIdConflict`。交付行记录
`original_bits`、原精确值/状态、`coefficient`、`scale`、精确误差，独立读回时
核实际表示而非重新选择当前观测。新观测/参考只会生成新的语义产品和新的交付，
旧产品与旧交付保持可重开读。

## 5. 材料与 J0/J1 的实测结果

`demo.py` 实际读取 `materials/739/input.json` 并执行全部操作：

* `3dcccccd`：`13421773/134217728`，比 `1/10` 大 `1/671088640`，零容差 `OUTSIDE`；
* `80000000`：负零，`sign=1`，对 `0` 为 `PASS`；
* `7fc00001`：`NAN`，`payload=4194305`；`7f800000`：正无穷；
* `00000001`：`2^-149`，零容差 `OUTSIDE`；
* 更正后 `s1=1/8`（`3e000000`）且 `r2` 参考 `1/8` → `PASS`；旧报告仍保留
  `3dcccccd` 与 `1/671088640`。

追加请求 `{s1,s2}`、`allowed_scales=[2,3]`、`max_abs_coefficient=99`：

* **J0**（`max_abs_error=0`）整体拒绝：scale 2 → 系数 12、误差 `1/200`（超误差界）；
  scale 3 → 系数 125、误差 0（越系数界）。
* **J1**（`max_abs_error=1/200`）成功：`s1` → `coefficient=12, scale=2`
  （读者值 `3/25`，误差 `−1/200`）；`s2` → `coefficient=0, scale=2`，保留
  `80000000` 与 `sign=1`。J0 被拒不影响语义服务继续查询 `s1` 的 `PASS`。

## 6. 热循环闭合与留样实物追溯

### 6.1 闭合语义

设备目录按应力等级声明 `(low, high)` 阈值对（严格 `low < high`）。每个等级
从冷态开始：读数**达到 high** 即进入热态并记下该升温和原件；此后**第一次达到
low** 完成一次循环、回到冷态，记下该降温和原件。读数严格处于两阈值之间时状态
不变；闭合读数属于该循环。判定一律用**原件精确有理值**，不经交付舍入。

分段边界带标记：`continuation` 把尚未闭合的热态（连同升温和原件）带入下一段；
`reload_reset` 让所有等级重新从冷态开始。因此闭合按等级跨段计算，而非分段各算。
`thermal.render`（`semantics.compute_closures`）按 `(闭合读数位置, 等级名)` 稳定
排序输出；多个等级可在同一观测时刻闭合，各自产生一条闭合事件。

`ThermalService.publish_product(product_id, catalog_version, readings, segments=…)`
固化「目录 × 冻结读数 × 闭合流」为不可变产品：

* 每条读数的 `tick` 必须严格唯一；`bits` 保留原始 binary32 位型；
* **任一非有限读数（NaN/±Inf）→ 整段报错**（`ThermalSegmentRejected`），
  不写任何产品，旧产品原样保留并可读回；
* 相同内容重放（`created=False`）；同 id 异内容 → `ThermalProductConflict`；
* 读回时重新校验定义摘要与闭合流摘要，篡改即 `IntegrityError`。

修订早先观测后以新 id 发布新产品即可；旧产品仍按位型读回（含原升/降温原件）。

### 6.2 实物留样器回放

`PhysicalService` 把冻结产品、版本化工位日志、转盘配置与一张化验单放在一起回放：

* 工位日志 `(position, levels, cooling)` 版本化；位置订阅若干等级，**同一时刻
  命中多个订阅只触发一次**（一次装样、转盘只前进一格）。
* 转盘有 N 个按序位置，各放唯一序号盒，当前指针每装一次前进一格并在末尾绕回
  `(p+1) % N`。
* 有效触发把样品送入当前盒并前进；**机械冷却窗口** `[上次装样, 上次+冷却时长)`
  内的新触发**直接漏采**，既不排队也不转盘；恰在截止点可再次采样（半开区间）。
* 换盒操作记录 `take` 实物序号，必须等于该位置当时的盒；**回放不能拿另一只盒
  顶替**；换上的盒必须是未用过的序号。**同刻先换盒再触发**（合并进同一时刻的
  事件顺序）。
* 同一只盒在未被换下前**不可重复装样**（`box_loaded_twice`）。

回放是纯函数、可重复：同一输入两次得到相同的装样/漏采/取下序列。任何物理矛盾
（取错序号、换盒序号已用过、盒被重复装样、初始序号重复等）抛出
`PhysicalTraceConflict` 并指出**第一处**实物不相符；不发布核对，最近一次可核对
交付保持可读。成功核对读回时重新回放并与存储投影及内容摘要比对。

给定化验单 `{box_serial, claims:[{tick, level?}]}`，逐条给出：`matched` /
`wrong_box` / `skipped_due_to_cooling` / `level_mismatch` / `no_event_at_tick`。
**温度或工位日志后来更正会生成新核对版本**（新产品/新工位日志 → 新
`reconciliation_id`），旧单当时结论仍可按原 id 回查。

`public.public_thermal_product` / `public_reconciliation` 生成独立文档，
`verify_public_thermal_product` / `verify_public_reconciliation` 无需数据库即可
复核摘要、装样唯一性、漏采冷却窗、取下序号与化验单结论一致性。

### 6.3 题例实测（`thermal_demo`）

基准读数 `0,4,1,4,0`（t=0..4，仅低1高3一级）在 t=2、t=4 各闭合一次，两格先后
为 X、Y，冷却 1，t=5 取下位置1的 Y；化验单所称 t=4 样品属于它 → `matched`。

* 把 t=2 读数更正为 2：仅 t=4 闭合，样品进入 X，取下的 Y 为空 → `wrong_box`。
* 回到原始观测、只把工位冷却改为 3：t=4 在冷却窗口 `[2,5)` 内被漏采，Y 同样为空
  但原因为 `skipped_due_to_cooling`（与更正观测的 `wrong_box` 可区分）。
* 组合演示：两级 A、B 同刻闭合只触发一次；两格转盘第三次装样绕回第 0 格，且
  同一时刻先换盒（X→W）再触发，故绕回后样品进入 W；关闭数据库重开，经正常读者
  入口复核产品、核对与化验单结论稳定一致。

## 7. 规模

测试覆盖 5000 观测（1000 样品 × 5 版）、100 个参考版本、单次 1000 样品的
语义产品与交付，以及独立读回校验；本地全部 < 2 秒。不做概率统计、浮点求和
或硬件异常模拟。
