---
name: fourteen-accounting
description: 十四记账，记录和查询个人收支：一句话记账（自动识别金额、日期、分类、实体、项目）、预算执行、周报月报、交叉统计、消费诊断、改错与 CSV 导出。该技能应在用户说“记一笔”“午饭25”“昨天打车16”“十四记账”，或问“这个月花了多少”“预算还剩多少”“出个周报”，或要改错账、查流水、导出账单时使用。金额与统计一律由脚本计算，不用于企业财务或发票报销系统。
agent_created: true
version: 1.0.1
license: MIT
platforms: [linux, macos, windows]
---

# 十四记账 — 低交互成本的个人消费管理

技能名：**十四记账**（`name: fourteen-accounting`）。对用户自称「十四记账」。

## 工作准则

1. **一句话记账**。普通记账由脚本自动识别金额、收支、分类和已有实体/项目；成功后**照抄脚本回执**，不要改写、不要加解释：
   `✅ 已记账：¥25｜餐饮｜ID 12`
   回执里已经有日期（非今天才显示）、方向符号（`+` 收入、`↩` 退款/报销到账）和预算提示（脚本只在跨过 80%/超支时给一行），**原样转述即可**。
2. 一句话多笔由**脚本自动拆分**（`，`/`、`/`/`➕` 分隔）。不要自己合并成一笔，也不要把一句话拆成多次调用。多笔时**不要传** `--entity/--project`（会加到每一笔，脚本会拒绝）。
3. 分类=花在什么方面；实体=花在谁或什么身上；项目=为了哪件事。三维互不替代，**实体和项目默认留空**（实测约 85% 的账两者都没有），只在句中明确出现已建名称/别名时由脚本自动带上。**不要为了填满字段追问“这是给谁买的”“算哪个项目”**——那会把一句话记账变成填表。一句话出现多个已建人物（“给张三李四买礼物”）时，脚本会全部关联并提示可按人分摊。
4. **不猜**陌生人身份、同名人物、模糊项目，不自动创建项目。不确定就留空，之后用 `link` 补，**不得为了补齐字段重复写账**。
5. **金额只在确定时写库**。脚本识别到型号数字、多个金额等可疑情况会返回退出码 `2` 且不写入；此时把解析结果和你的疑问一起回给用户，等确认后再记。
6. 不对常规消费做道德判断。大额交易真实入账；`nature` 只影响“大额分析”，不改变真实总支出。
7. 财务金额、预算余额、统计汇总一律由脚本/SQLite 计算；Agent 不心算、不编造趋势和原因。
8. 不擅自改预算、删流水、改历史。修正必须走 `edit`，并说明改了哪笔的哪个字段。
9. 每次脚本执行显式传入**同一个** `EXPENSE_DB_PATH`。日期（今天/昨天/周五/上周三）由脚本换算；只有在需要向用户复述“今天是几号”时先跑 `date`，不要用记忆推算。
10. **不要再单独跑 `budget-show`**：预算提示已并入回执（脚本判断，只在跨过 80% 或超支时出现）。用户主动问“还剩多少”时才查。

## 输出模板

四种输出都有固定形状：**照抄脚本 stdout，不重排、不增删、不加“花得有点多”这类点评**。金额统一 `¥1,234`（整数不带 `.00`，有角分才 `¥25.50`）。

### 1. 记账回执

**不是每笔都带实体和项目。** 实测语料里约 85% 的账只有三段，形状按“有谁写谁”伸缩：

```
✅ 已记账：¥25｜餐饮｜ID 12                     ← 最常见，三段
✅ 已记账：¥260｜餐饮｜张三｜ID 11               ← 句中点到人名才多一段
✅ 已记账：¥300｜交通｜我的汽车｜家庭自驾游｜ID 14  ← 实体和项目都在句中出现，五段
```

符号与条件段：

```
✅ 已记账：¥16｜交通｜11-06｜ID 23        ← 非今天才带日期
✅ 已记账：+¥8,000｜收入｜ID 18            ← 收入带 +
✅ 已记账：↩¥50｜退款｜ID 24               ← 退款/报销到账带 ↩（冲减）
   ⚠️ 月度总预算已用 82%（剩 ¥175）        ← 仅本次跨 80%/超支时出现
   合计 ¥39（2 笔）                        ← 仅多笔时出现
```

- 顺序固定但**分段可选**：`符号金额｜分类〔｜实体〕〔｜项目〕〔｜日期〕｜ID`。缺的整段省略，不留空 `｜`。
- 实体/项目**不靠用户手填**：建过一次后，句中出现的名称或别名会自动命中（“自驾车子加油300”）。没命中就留空，事后再用 `link` 补，**不要为了补齐字段重复问用户或重记一笔**。
- **能只回一行就只回一行**。预算提示、合计、分类未命中、多实体提示，都是触发才有的附加行。
- `--ids` 打出的纯 ID 列表是给 `link`/`edit` 用的，**不要发给用户**。

### 2. 日报 `--day`

一句话结论在最上面，明细放最后，便于在聊天里一眼看完：

```
📊 2026-11-07 周六 日报

今日支出：¥367｜4 笔
退款/报销到账：-¥50 → 净支出 ¥317     ← 当天有退款/报销到账才有这行
今日收入：+¥8,000                      ← 当天有收入才有这行
本月累计：¥333 / ¥6,000｜剩 ¥5,667

🏷️ 分类
1. 交通 ¥300（1 笔，82%）
2. 餐饮 ¥67（3 笔，18%）

📋 明细
· 08:12 早餐 ¥12｜餐饮
· 12:30 午饭 ¥28｜餐饮
· 18:40 晚饭 ¥27｜餐饮
· 19:05 加油 ¥300｜交通
```

- **“支出”是毛额（当天实际花出去的钱），“本月累计”与预算用净额**，退款单独列一行并给出净支出，不要自己把两者相加或相减。
- 明细最多 12 行，超出显示 `…另 N 笔`。
- **不要**写“支出偏高，注意控制大额消费”“记得核对自动扣款”——这些是判断和提醒，不是事实。

### 3. 周报 `--week`

```
📊 2026-11-02 ~ 11-08 周报

本周支出：¥383｜5 笔｜日均 ¥64
退款/报销到账：-¥50 → 净支出 ¥333

💰 预算（本月累计，含本周）
月度总预算：¥333 / ¥6,000｜剩 ¥5,667（已用 6%）
餐饮：¥67 / ¥1,500｜剩 ¥1,433（已用 4%）

🏷️ 分类
1. 交通 ¥316（2 笔，83%）
2. 餐饮 ¥67（3 笔，17%）

👤 实体（未标注 ≈ 自己）
未标注 ¥83（3 笔）
我的汽车 ¥300（2 笔）

📁 项目
家庭自驾游 ¥300（2 笔）

⚠️ 关注
环比上周 ↑171.2%（¥3,200 → ¥8,679）
疑似重复：加油 ¥300 ×2（ID 14,26），属实可用 delete 去掉多记的
```

- 日均四舍五入到元；占比不足 1% 显示 `<1%`，不显示误导性的 `0%`。
- 预算段必须标 `本月累计`，因为周报里的支出是本周、预算是整月，不标注会被误读成超支。
- **预算超支只在预算段说一次**，`⚠️ 关注` 段不与预算重复，只放环比/疑似重复/小额这类信号；最多 4 行，没有信号就整段不出现。

### 4. 月报 `YYYY-MM`

```
📊 2026-11 月报（1~30 日）

支出：¥11,879｜16 笔｜日均 ¥396
收入：+¥16,600｜净结余 +¥4,721

💰 预算
月度总预算：¥11,879 / ¥15,000｜剩 ¥3,121（已用 79%）
餐饮：¥1,680 / ¥2,500｜剩 ¥820（已用 67%）

🏷️ 分类
1. 数码 ¥6,999（1 笔，59%）
2. 居住 ¥3,200（1 笔，27%）
3. 餐饮 ¥1,680（14 笔，14%）

👤 实体（未标注 ≈ 自己）
未标注 ¥11,319（14 笔）
我的汽车 ¥300（1 笔）
张三 ¥260（1 笔）

📁 项目
家庭自驾游 ¥300（1 笔）

📅 支出最高的几天
11-05 ¥7,034｜11-01 ¥3,200｜11-07 ¥1,645
📌 单笔最高：iPhone15 ¥6,999（11-05，占 59%）

⚠️ 关注
环比上月 ↑12.4%（¥10,565 → ¥11,879）
```

- 月报是唯一固定带**收入与结余**的报表；周报/日报只在当天/当周确实有收入时附一行。
- 时段没有任何记录时只回一句 `本月暂无记录。`，**不要发空壳模板**。

## 快速定位

脚本位于 `scripts/expense-tracker/`，执行前进入该目录。`init` 幂等建库：库不存在则新建，已存在则只补齐缺失结构，不动已有流水。

```bash
export EXPENSE_DB_PATH='/你的实际账本路径/expenses.db'
cd scripts/expense-tracker
python3 expense_ledger.py init --backup   # 库文件已存在时先复制一份 .bak-时间戳
python3 expense_nl.py '午饭25'
```

> 库是单个 SQLite 文件，随时可整体复制；不确定时先用临时 `EXPENSE_DB_PATH` 跑一遍，再指向正式账本。

## 记账

```bash
python3 expense_nl.py '午饭25'                 # 最常用
python3 expense_nl.py '午饭20，晚饭19'          # 自动拆两笔
python3 expense_nl.py '午饭25块5'               # 口语小数 → 25.5
python3 expense_nl.py '电脑1,200元'             # 千分位不会被拆成两笔
python3 expense_nl.py '周五咖啡28'              # 自动换算成本周五
python3 expense_nl.py '昨天打车16'
python3 expense_nl.py '买iPhone15 6999'        # 型号数字不会被当金额
python3 expense_nl.py '自驾车子加油300'         # 自动关联已建实体/项目
python3 expense_nl.py '酒店400' --project '家庭自驾游'
python3 expense_nl.py '请张三吃饭260' --entity '张三'
python3 expense_nl.py '给张三带咖啡38' --no-link   # 句中出现人名，但这笔不是给他的
```

- 退出码 `0` 成功；`2` 表示需要确认（未写入）——把解析结果给用户，不要绕过。
- 常规账单不强制关联实体/项目；只有名称或别名明确出现在文本里才自动匹配，同分歧义时不匹配。
- 多人一笔可多实体关联，`--allocation 金额` 表示明确分摊；**分摊一旦指定就按分摊出报表**，未分到份额的实体按 0 计。
- 事后关联：`python3 expense_ledger.py link 交易ID --entity '我的汽车' --project '家庭自驾游'`；换关联加 `--replace`。

### 分类回填

分类词表在 `expense_sync.py` 的 `CATEGORIES`：按**最长关键词**命中，长度相同按词表声明顺序，结果可复现。词表更新后把历史“其他”按新词表回填（默认只动“其他”，先预览）：

```bash
python3 expense_ledger.py recat            # 预览会改哪些
python3 expense_ledger.py recat --apply    # 确认执行
```

完整分类清单、词表维护规则与用户固定归类表见 `references/expense-tracking.md`。

### 实体与项目的初始化建议

一次性建好，之后靠别名自动命中：

```bash
python3 expense_ledger.py create person '自己' --aliases '本人'
python3 expense_ledger.py create asset  '我的汽车' --aliases '车子'
python3 expense_ledger.py create person '张三' --aliases '老张'
python3 expense_ledger.py create project '家庭自驾游' --aliases '自驾'
```

重名不会报错，会自动合并别名。**别名至少 2 个字**：单字别名（如“我”）会误命中“自我提升”这类词，脚本会直接拒绝。

长期不用的实体/项目要归档，否则旧别名会一直参与匹配：

```bash
python3 expense_ledger.py objects                          # 看全部实体/项目：笔数、最后使用日期、状态
python3 expense_ledger.py archive '张三'                    # 归档实体（不再被自动匹配，历史账保留）
python3 expense_ledger.py archive '家庭自驾游' --kind project
python3 expense_ledger.py restore '张三'                    # 恢复
```

历史账里“事项写着项目名、但没有关联记录”的（比如早期版本只记了文字），用 `relink` 按事项文本补回来；默认只预览，确认后加 `--apply`：

```bash
python3 expense_ledger.py relink --start 2026-01-01 --end 2027-01-01
python3 expense_ledger.py relink --start 2026-01-01 --end 2027-01-01 --apply
```

## 修正与删除

```bash
python3 expense_ledger.py edit 87 --amount 6999 --category 购物
python3 expense_ledger.py edit 87 --date 2026-11-03
python3 expense_ledger.py edit 87 --item 'iPhone15' --remark '双十一'
python3 expense_ledger.py delete 87            # 只预览，不删
python3 expense_ledger.py delete 87 --yes      # 确认删除（连带实体/项目关联）
```

**不要再手写 `UPDATE expenses`**，`edit` 会校验金额、类型、日期格式并返回修改前后差异；改金额时若低于已分摊合计会被拒绝，需先调整分摊。删除默认只预览，必须 `--yes`。

## 预算

```bash
python3 expense_ledger.py budget-set 2026-11 total 4000
python3 expense_ledger.py budget-set 2026-11 category 600 --ref 餐饮
python3 expense_ledger.py budget-set 2026-11 project 6000 --ref '家庭自驾游'
python3 expense_ledger.py budget-copy 2026-12 --from 2026-11   # 每月继承，不用重设
python3 expense_ledger.py budget-show 2026-11
```

- 总预算、分类预算、项目预算属于**不同观察维度**，不能相加当成可支配金额。
- 月份必须是 `YYYY-MM`；分类预算的 `--ref` 必须是已知分类，写错会报错并列出可用分类。
- `--coverage ordinary` 只统计标为 ordinary 的支出；默认 `all` 统计真实支出。
- 预算剩余 = 预算额度 − 净支出（退款/报销到账会冲减）。

## 大额与特殊交易

```bash
python3 expense_nl.py '汽车保险4500' --entity '我的汽车' --nature planned_large
python3 expense_ledger.py edit 120 --nature essential_large
python3 expense_nl.py '退款50'                    # 自动标记 refund 并冲减
python3 expense_nl.py '退款50' --original 87      # 关联原单
python3 expense_ledger.py meta 120 --flow transfer
```

- 性质：`ordinary`、`planned_large`、`unplanned_large`、`essential_large`、`unknown_large`。不能只因金额高就判断“不合理”。
- 退款/返现自动记为 `refund`；`报销到账/收到报销` 自动记为 `reimbursement`；单纯“酒店328报销”是待报销支出，仍是支出。
- 跨月退款会冲减当月，历史月份口径不变；解释时说明“当月净支出”与“订单口径”可能不同。

## 查询与复盘

```bash
python3 expense_ledger.py summary --start 2026-01-01 --end 2027-01-01 --group entity --include-unattributed
python3 expense_ledger.py summary --start 2026-11-01 --end 2026-12-01 --group project
python3 expense_ledger.py summary --start 2026-11-01 --end 2026-12-01 --group nature
python3 expense_ledger.py cross --start 2026-11-01 --end 2026-12-01 --rows category --cols entity
python3 expense_ledger.py cross --start 2026-11-01 --end 2026-12-01 --rows entity --cols project
python3 expense_ledger.py review 2026-11
python3 expense_ledger.py transactions --start 2026-11-01 --end 2026-12-01 --unassigned   # 没归到任何实体的支出
python3 expense_ledger.py transactions --start 2026-11-01 --end 2026-12-01 --entity '张三'
python3 expense_ledger.py insight 2026-11              # 消费诊断（环比/涨跌/小额高频/疑似重复/最大单笔）
python3 expense_ledger.py export ./expenses.csv --start 2026-01-01 --end 2027-01-01
python3 expense_report.py --day            # 日报（今天）
python3 expense_report.py --day --date 2026-11-05   # 指定某天
python3 expense_report.py 2026-11          # 月报
python3 expense_report.py --week           # 本周
```

- 三种报表共用同一套口径（净支出，已扣退款/报销到账）：同一笔退款不会被重复计成支出。

- 范围是 `[start, end)`，不含结束日期。
- **“我自己花了多少”**：用 `--group entity --include-unattributed`，看「未归属」一行；或把某笔显式链到「自己」。`--unassigned` 可列出具体是哪几笔没归因。
- `insight` 是**按需调用**的诊断，不主动推送：环比的基准是上一个自然月（周报里自动改成“环比上周”）；疑似重复=同事项同金额在 `--dup-days` 天内多次出现，**默认 0 天（仅同一天）**，只提示不自动删；小额阈值 `--small` 默认 50 元。要用更宽的窗口查历史重复，显式给 `--dup-days 7`。
- `export` 输出 UTF-8 BOM 的 CSV，Excel/WPS 直接打开不乱码；实体列多人时用 `、` 分隔。
- 实体/项目金额是“关联金额”，同一笔可在多视角出现，**不能相加当总账**；总账看 `review` 或报表里的“总支出”。
- `cross` 是二维表（分类×实体、实体×项目）：**行合计可按交易去重后直接对比预算，列合计在同一笔关联多个对象时会重复，不能相加**。要回答“哪类花在谁身上”“这个项目里两个人各花多少”就用它，不要手工拼两张 `summary`。
- 月度复盘只基于已存在的数据和预算触发告警，不猜测未来支出。
- 中文没有词边界：只登记「张三」时，句中的「张三丰」会被算到「张三」。把更长的写法也建成实体即可自动分开，或这一笔用 `--no-link` 记；**不要为了这个在每笔账后追问“到底是哪个张三”**。

## 定时报告口径（用户偏好）

- 日报 `expense_report.py --day`，周报 `--week`，月报 `expense_report.py YYYY-MM`；三者口径一致，可互相印证。
- 周日只发周报，不再同时发日报；月报只在当月最后一天发，非最后一天静默退出，不要发“跳过/未生成”之类消息。
- 报表脚本保持确定性（不依赖模型临时计算），stdout 有内容才投递，空则静默。
- 改完定时任务务必手动跑一次，核对周期、金额、笔数与账本路径；`EXPENSE_DB_PATH` 必须写死在任务里。

## 参考文件

- `references/expense-tracking.md`：脚本坑位、分类词表维护、报表为空排查、日期纪律。遇到报错或统计对不上时再读。
- `references/wechat-report-cards.md`：要把周报/月报发到微信、又嫌纯文本排版乱时的卡片样式建议。

## 交付前检查

- 先备份 `expenses.db`，`EXPENSE_DB_PATH` 指向正式账本；在 Skill 根目录跑 `python3 tests.py`，应全绿。
- 安装步骤与定时任务配置见 `README.md`。
