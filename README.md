# 十四记账

一句话记账的个人消费账本。数据存在本地一个 SQLite 文件里，无账号、无网络、无云同步。

记一笔只要一句话：`午饭25`、`昨天打车16`、`自驾车子加油300`。金额、日期、分类，以及句中出现的实体与项目，都由脚本解析。解析不确定时它**不写库**，先把候选金额列出来问你——宁可多问一句，也不静默记错账。

## 能做什么

- **一句话记账**：自动识别金额、收支方向、分类、日期（今天/昨天/周五/上周三/大前天）；一句话多笔自动拆分。已建的实体与项目按名称或别名自动关联，一句话里出现多个明确人物会全部关联。
- **三个维度看账**：分类（花在什么方面）、实体（花在谁或什么身上）、项目（为了哪件事）。互不替代，可单独统计，也可交叉筛选；支持「未归属」口径，回答“我自己花了多少”。
- **预算**：月度总预算、分类预算、项目预算，支持按月继承；记账回执里只在**本次跨过 80% 或超支**时追加一行提示。
- **复盘**：日报 / 周报 / 月报共用一套口径（净支出，已扣退款与报销到账），含预算执行、构成占比、日均、单笔最高、消费诊断（环比、分类涨跌、小额高频、疑似重复）。
- **维护**：改错（`edit`）、删除（默认只预览）、账单关联（`link`）、实体与项目归档、分类回填（`recat`）、CSV 导出（UTF-8 BOM，Excel/WPS 打开不乱码）。
- **金额只用整数分存储**，报表由脚本汇总，不靠模型心算。

## 目录结构

```
fourteen-accounting/
├── SKILL.md                     行为准则与输出模板（必读，触发时加载）
├── README.md                    本文件：安装、上手、限制
├── LICENSE.txt                  MIT
├── references/
│   ├── expense-tracking.md      脚本坑位、分类词表维护、报表为空排查
│   └── wechat-report-cards.md   把周报/月报发到微信的卡片样式建议
├── scripts/expense-tracker/
│   ├── expense_nl.py            自然语言记账入口
│   ├── expense_ledger.py        账本管理：实体、项目、预算、查询、导出
│   ├── expense_report.py        日报 / 周报 / 月报
│   └── expense_sync.py          底层写入与分类词表
└── tests.py                     75 项自检，安装后先跑
```

`SKILL.md` 只放行为准则、命令与输出模板；细节与踩坑记录放 `references/`，同一信息不在两处各写一遍。

## 安装

1. 解开 `fourteen-accounting.zip`，把整个 `fourteen-accounting/` 目录放进 Agent 的 Skill 目录（如 `~/.workbuddy/skills/`）。**目录名要与 frontmatter 的 `name` 一致**，否则加载不到。
2. 指定账本文件：`export EXPENSE_DB_PATH=/绝对路径/expenses.db`。每次调用都用同一个路径，换路径等于换账本。
3. 在 `scripts/expense-tracker/` 下运行 `python3 expense_ledger.py init --backup`。
4. 在 Skill 根目录运行 `python3 tests.py`，75 项应全绿。
5. 需要定时复盘时自行配置任务：日报 `expense_report.py --day`、周报 `--week`、月报 `expense_report.py YYYY-MM`。本包**不自动创建推送任务**。

## 上手

```bash
export EXPENSE_DB_PATH="$HOME/expense-ledger/expenses.db"
cd scripts/expense-tracker

python3 expense_ledger.py init --backup     # 建库（已有则只补缺失结构）

python3 expense_nl.py '午饭25'               # ✅ 已记账：¥25｜餐饮｜ID 1
python3 expense_nl.py '午饭20，晚饭19'        # 自动拆两笔，末尾给合计
python3 expense_nl.py '昨天打车16'
python3 expense_nl.py '周五咖啡28'

python3 expense_ledger.py create asset '我的汽车' --aliases '车子'
python3 expense_ledger.py create project '家庭自驾游' --aliases '自驾'
python3 expense_nl.py '自驾车子加油300'       # 自动关联实体与项目

python3 expense_ledger.py budget-set 2026-11 total 4000
python3 expense_report.py --day
python3 expense_report.py 2026-11
```

第一次用建议先只建预算、记几天账，再看报表是否符合自己的口径。分类词表在 `expense_sync.py` 的 `CATEGORIES`，按最长关键词命中，长度相同按声明顺序，结果可复现。

## 数据

- 全部数据在一个 SQLite 文件里，直接复制就是完整备份；`init --backup` 会在库已存在时先复制一份 `.bak-时间戳`。
- 表格结构：`expenses`（流水）、`entities` / `projects`（实体与项目）、`transaction_entities` / `transaction_projects`（关联与分摊）、`transaction_meta`（收支类型、大额性质、原单）、`budgets`（预算）。
- 早期账本文件里带前缀的旧表名会在 `init` 时自动改名并入，实体与预算不会丢。

## 设计取向

- **不追问**。实体与项目是可选段，只在句中明确出现已建名称/别名时才带上。约 85% 的账只有分类，追问“这是给谁买的”会把一句话记账变成填表。
- **不猜**。陌生身份、同名人物、模糊项目一律留空，事后用 `link` 补，不重复写账。
- **不评判**。大额消费真实入账，不输出“花得有点多”这类点评；`nature` 只影响大额分析，不改变真实总支出。
- **不算命**。金额、余额、趋势一律由脚本算；没有数据就说“暂无记录”，不编造原因。

## 已知限制

- 未集成外部账单同步、Web 看板、银行卡余额或自动推送。
- 一句话多笔按 `，、；➕` 拆分；用空格或“和”连接的多笔仍会被当一笔，需分开说。
- 诊断阈值是固定默认值（小额 50 元、疑似重复仅同一天、环比 20%），属于通用起点，尚未按个人历史分布自适应。
- 多实体关联默认按“关联金额”全记（同一笔在两人名下各显示全额），要按比例算需 `link --allocation`。
- 跨周期冲销、多账户对账未自动化；退款冲减当月，历史月份不回溯。
- 实体/项目排行是“关联账单金额”，多视角会重复展示，**不能相加当作实际总支出**。

更多指令与行为约束见 [SKILL.md](SKILL.md)。
