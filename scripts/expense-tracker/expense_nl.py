#!/usr/bin/env python3
"""
自然语言记账入口（十四记账）。

设计要点：
1. 金额解析不取“第一个数字”：优先带单位/句末数字，型号里的数字（iPhone15）不会被当金额；
   模棱两可时进入确认，不静默写库。
2. 一句话多笔自动拆分：`午饭20，晚饭19` / `夜宵41➕17` 各记一笔，不合并丢钱。
3. 支持周几（周五/上周三）与大前天，日期由脚本算，不依赖记忆。
4. 退款/返现自动标记 flow=refund 并冲减预算，无需再手工 meta。
5. 输出压缩为一行确认，便于微信里阅读：非今天带日期、收入带 `+`、退款带 `↩`，
   实体与项目按句中命中情况可选出现，预算提示只在本次跨 80%/超支时追加。

写库仍然只走 expense_sync.add_expense。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from expense_sync import add_expense, auto_categorize  # noqa: E402

INCOME_WORDS = ["收入", "入账", "到账", "工资", "奖金", "退款", "返现", "返款"]
REIMBURSE_WORDS = ["报销", "可报销", "待报销"]

# 退款类只认明确“钱回来”的说法；单纯的“报销”是待报销支出，不能冲减。
REFUND_RE = re.compile(r"(退款|返现|返款|退回|退差价)")
REIMBURSE_IN_RE = re.compile(r"(报销到账|报销款到账|收到报销|报销回来|报销已到账)")

NUM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])")
UNIT_AFTER_RE = re.compile(r"[ \t]*(元|块钱|块|RMB|rmb|¥|￥)")
MULT = {"万": 10000, "w": 10000, "k": 1000}

DATE_PATTERNS = [
    re.compile(r"(?P<year>20\d{2})[年/-](?P<month>\d{1,2})[月/-](?P<day>\d{1,2})日?"),
    re.compile(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"),
]
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WEEKDAY_RE = re.compile(r"(上上|上|本|这)?(?:周|星期|礼拜)([一二三四五六日天])")

# 一句话多笔：中文逗号/顿号/分号/加号/换行
SEP_RE = re.compile(r"[，,、；;➕＋+]|\n+")


@dataclass
class ParsedExpense:
    text: str
    amount: float | None
    type: str
    category: str
    item: str
    remark: str
    expense_time: str | None
    confidence: float
    needs_confirm: bool
    reason: str
    flow: str | None = None
    warnings: list = field(default_factory=list)
    entity: str | None = None
    project: str | None = None


def _clean_amount(amount: float):
    return int(amount) if float(amount).is_integer() else amount


def parse_date(text: str, now: datetime | None = None):
    """返回 (expense_time, 去掉日期词后的文本, 命中的日期词)。"""
    now = now or datetime.now()
    text = text.strip()

    def take(days: int, token: str):
        cleaned = text.replace(token, "", 1).strip(" ，,、")
        when = (now - timedelta(days=days)).strftime("%Y-%m-%d 12:00:00")
        return when, cleaned, token

    if "大前天" in text:
        return take(3, "大前天")
    if "前天" in text:
        return take(2, "前天")
    if "昨天" in text:
        return take(1, "昨天")
    if "今天" in text:
        cleaned = text.replace("今天", "", 1).strip(" ，,、")
        return None, cleaned, "今天"

    m = WEEKDAY_RE.search(text)
    if m:
        target = WEEKDAY[m.group(2)]
        delta = (now.weekday() - target) % 7
        prefix = m.group(1) or ""
        if prefix == "上":
            delta += 7
        elif prefix == "上上":
            delta += 14
        cleaned = (text[: m.start()] + text[m.end():]).strip(" ，,、")
        d = now - timedelta(days=delta)
        return d.strftime("%Y-%m-%d 12:00:00"), cleaned, m.group(0)

    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        year = int(m.groupdict().get("year") or now.year)
        month = int(m.group("month"))
        day = int(m.group("day"))
        try:
            d = datetime(year, month, day)
        except ValueError:
            continue
        cleaned = (text[: m.start()] + text[m.end():]).strip(" ，,、")
        return d.strftime("%Y-%m-%d 12:00:00"), cleaned, m.group(0)
    return None, text, None


def _amount_candidates(text: str):
    """列出所有数字，并标注是否带单位、是否粘在型号里（如 iPhone15）。"""
    out = []
    for m in NUM_RE.finditer(text):
        start, end = m.span()
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        attached = bool(re.match(r"[A-Za-z]", before or "")) or bool(re.match(r"[A-Za-z]", after or ""))
        value = float(m.group(1))
        unit = bool(UNIT_AFTER_RE.match(text[end:]))
        end_full = end
        # 25块5 / 25块50 → 25.5 / 25.5（口语小数）
        frac = re.match(r"[ \t]*块(?:钱)?[ \t]*(\d{1,2})(?!\d)", text[end:])
        if frac:
            digits = frac.group(1)
            value += int(digits) / (10 ** len(digits))
            unit = True
            end_full = end + frac.end()
        nxt = text[end: end + 1]
        mult = MULT.get(nxt, 1)
        if mult != 1:
            unit = True
            end_full = end + 1
            # 1.2万 的“万”已被包含，金额取整
        out.append({
            "raw": m.group(1), "value": value * mult, "unit": unit, "attached": attached,
            "start": start, "end": end, "end_full": end_full,
            "span": text[start:end_full],
        })
    return out


def extract_amount(text: str):
    """返回 (amount, 金额原文, 是否需要确认, 原因, 提示列表)。"""
    cands = _amount_candidates(text)
    if not cands:
        return None, None, True, "缺少金额", []
    clean = [c for c in cands if not c["attached"]]
    notes = []
    if not clean:
        return None, None, True, "只找到型号里的数字，请明确金额", ["疑似型号数字，未取用"]
    if len(cands) > len(clean):
        notes.append("已忽略型号中的数字")

    if len(clean) == 1:
        pick, ambiguous = clean[0], False
    else:
        with_unit = [c for c in clean if c["unit"]]
        if with_unit:
            pick, ambiguous = with_unit[-1], False
        else:
            pick, ambiguous = clean[-1], True
            notes.append("出现多个金额，已取最后一个")

    value = pick["value"]
    if value <= 0:
        return None, None, True, "金额必须大于 0", notes
    if value >= 1_000_000:
        ambiguous = True
        notes.append("金额过大，请复核")
    return value, pick["span"], ambiguous, ("存在多个金额，请确认" if ambiguous else "ok"), notes


def infer_type(text: str) -> str:
    if any(w in text for w in INCOME_WORDS):
        if "报销" in text and not any(w in text for w in ["收入", "入账", "到账", "收到", "回款"]):
            return "支出"
        return "收入"
    return "支出"


def detect_flow(text: str) -> str | None:
    if REIMBURSE_IN_RE.search(text):
        return "reimbursement"
    if REFUND_RE.search(text):
        return "refund"
    return None


def normalize_item(text: str, amount_text: str | None, expense_type: str = "支出") -> str:
    item = text
    if amount_text:
        item = item.replace(amount_text, "", 1)
    for token in ["收入", "支出", "消费", "花了", "付了", "元", "块钱", "块", "RMB", "rmb", "¥", "￥"]:
        item = item.replace(token, "")
    if expense_type == "支出":
        for token in REIMBURSE_WORDS:
            item = item.replace(token, "")
    item = re.sub(r"\s+", " ", item).strip(" ，,。;；")
    if item:
        return item
    if expense_type == "收入":
        return "报销" if "报销" in text else "收入"
    return "未命名"


def parse_expense(text: str, now: datetime | None = None) -> ParsedExpense:
    raw = text.strip()
    expense_time, text_wo_date, _ = parse_date(raw, now=now)

    amount, amount_text, amount_doubt, amount_reason, notes = extract_amount(text_wo_date)
    expense_type = infer_type(text_wo_date)
    flow = detect_flow(text_wo_date)
    if flow in ("refund", "reimbursement"):
        expense_type = "收入"
    item = normalize_item(text_wo_date, amount_text, expense_type)
    category = "收入" if expense_type == "收入" else auto_categorize(item)

    remark_parts = []
    if any(w in raw for w in REIMBURSE_WORDS) and expense_type == "支出":
        remark_parts.append("报销")
    remark = "；".join(remark_parts)

    needs_confirm = False
    reasons = []
    confidence = 0.95
    if amount is None:
        needs_confirm = True
        reasons.append(amount_reason or "缺少金额")
        confidence = 0.2
    elif amount_doubt:
        needs_confirm = True
        reasons.append(amount_reason)
        confidence = 0.6
    if item == "未命名":
        needs_confirm = True
        reasons.append("缺少项目")
        confidence = min(confidence, 0.5)
    if category == "其他":
        confidence = min(confidence, 0.75)
        reasons.append("分类未命中，使用其他")

    return ParsedExpense(
        text=raw, amount=amount, type=expense_type, category=category, item=item,
        remark=remark, expense_time=expense_time, confidence=confidence,
        needs_confirm=needs_confirm, reason="；".join(reasons) if reasons else "ok",
        flow=flow, warnings=notes,
    )


def split_items(text: str):
    """一句话多笔拆分。只有每段都含数字时才拆，避免把‘午饭，加个鸡腿’拆坏。"""
    segs = [s.strip() for s in SEP_RE.split(text) if s.strip()]
    if len(segs) <= 1:
        return [text.strip()]
    if all(re.search(r"\d", s) for s in segs):
        return segs
    return [text.strip()]


def _money(value) -> str:
    d = Decimal(str(value))
    return f'{int(d):,}' if d == d.to_integral_value() else f'{d:,.2f}'


def _confirm_line(p: ParsedExpense, record_id=None, today=None) -> str:
    """回执单行：符号 + 金额｜分类｜实体｜项目｜日期(非今天)｜ID。

    符号区分资金方向，避免“退款50”看起来像又花了一笔：
    支出无符号、收入 `+`、退款/报销到账 `↩`（冲减）。
    """
    sign = ''
    category = p.category
    if p.flow == 'refund':
        sign, category = '↩', '退款'
    elif p.flow == 'reimbursement':
        sign, category = '↩', '报销到账'
    elif p.type == '收入':
        sign, category = '+', category
    parts = [f'{sign}¥{_money(p.amount)}', category]
    if p.entity:
        parts.append(p.entity)
    if p.project:
        parts.append(p.project)
    day = (p.expense_time or '')[:10]
    if day and day != (today or date.today().isoformat()):
        parts.append(day[5:])          # 非今天才标日期，方便核对“昨天/周五”换算对不对
    if record_id is not None:
        parts.append(f'ID {record_id}')
    return '｜'.join(parts)


def _amt(value) -> str:
    return f'¥{_money(value)}'


def _budget_hint(entries) -> list[str]:
    """预算提示只是附加信息，任何异常都不应影响记账本身。"""
    try:
        return _budget_hint_body(entries)
    except Exception:
        return []


def _budget_hint_body(entries) -> list[str]:
    """写账后提示预算：只在**本次记账跨过 80% 或超支**时出现，避免整月重复刷屏。

    entries: [(category, 净额变动元)]，退款/报销为负数。
    项目预算不参与，避免多笔归因误差；项目执行看周报/月报。
    """
    try:
        from expense_ledger import budget_status
        rows = budget_status(date.today().strftime('%Y-%m'))
    except Exception:
        return []
    net_delta = Decimal('0')
    cat_delta: dict[str, Decimal] = {}
    for cat, delta in entries:
        d = Decimal(str(delta))
        net_delta += d
        cat_delta[cat] = cat_delta.get(cat, Decimal('0')) + d

    total_line, others = '', []
    for b in rows:
        budget, spent = Decimal(b['budget']), Decimal(b['spent'])
        if budget <= 0 or b['scope'] == 'project':
            continue
        delta = net_delta if b['scope'] == 'total' else cat_delta.get(b['name'], Decimal('0'))
        before = spent - delta
        crossed_up = spent > budget and before <= budget
        crossed_warn = spent / budget >= Decimal('0.8') and before / budget < Decimal('0.8')
        if not (crossed_up or crossed_warn):
            continue
        if b['scope'] == 'total':
            if spent > budget:
                total_line = f"⚠️ 月度总预算已超 {_amt(spent - budget)}（{_amt(spent)} / {_amt(budget)}）"
            else:
                total_line = f"⚠️ 月度总预算已用 {spent / budget:.0%}（剩 {_amt(budget - spent)}）"
        elif spent > budget:
            others.append(f"{b['name']} 超支 {_amt(spent - budget)}")
        else:
            others.append(f"{b['name']} 已用 {spent / budget:.0%}")
    out = []
    if total_line:
        out.append('   ' + total_line)
    if others:
        out.append('   ⚠️ ' + '；'.join(others[:2]))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="自然语言记账入口")
    ap.add_argument("text", nargs="+", help="一句自然语言账目")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不写入")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--ids", action="store_true", help="额外输出一行 ID 列表（供脚本/后续 link 使用）")
    ap.add_argument("--entity", help="明确关联已创建的实体名称")
    ap.add_argument("--project", help="明确关联已创建的项目名称")
    ap.add_argument("--nature", choices=["ordinary", "planned_large", "unplanned_large",
                                         "essential_large", "unknown_large"], help="交易属性，非必填")
    ap.add_argument("--flow", choices=["expense", "income", "transfer", "refund", "reimbursement"],
                    help="资金流性质，非必填；退款/报销到账会自动识别")
    ap.add_argument("--original", type=int, help="退款/报销对应的原交易 ID")
    ap.add_argument("--no-link", action="store_true",
                    help="不做自动关联（句中出现人名/项目名但这笔不是给他的时候用）")
    args = ap.parse_args(argv[1:])

    text = " ".join(args.text)
    # 千分位（1,200）不是多笔分隔符，先合并，避免被拆成 1 和 200
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    _, _, date_phrase = parse_date(text)
    segments = split_items(text)

    if args.no_link and (args.entity or args.project):
        print("--no-link 与 --entity/--project 互斥：要么自动关联，要么明确指定。", file=sys.stderr)
        return 2

    if len(segments) > 1 and (args.entity or args.project):
        print(f"一句话包含 {len(segments)} 笔，--entity/--project 会误加到每一笔。"
              "请分开记，或只在文本里写别名让它自动匹配。", file=sys.stderr)
        return 2

    parsed: list[ParsedExpense] = []
    prev_label = ""
    for seg in segments:
        seg_text = seg if (not date_phrase or date_phrase in seg) else date_phrase + seg
        # “夜宵41➕17”这类后段只有金额时，沿用前段的事项名，避免要求用户重发
        label = re.sub(r"[\d.]+", "", seg_text).strip(" ，,、元块钱¥￥")
        if not label and prev_label:
            seg_text = prev_label + re.sub(r"^[^\d]*", "", seg_text)
        parsed.append(parse_expense(seg_text))
        prev_label = label or prev_label

    blocking = [p for p in parsed if p.needs_confirm]
    if args.dry_run or blocking:
        payload = {"items": [asdict(p) for p in parsed], "count": len(parsed),
                   "needs_confirm": bool(blocking)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for p in payload["items"]:
                print(f"解析：{p}")
        if blocking:
            print("未写入：请先确认上述金额/项目，再重发（或先 --dry-run 复核）。", file=sys.stderr)
            return 2
        return 0

    # 先校验实体/项目，避免写一半
    if args.entity or args.project:
        from expense_ledger import connect, init as init_ledger, _resolve
        init_ledger()
        with connect() as con:
            if args.entity:
                _resolve(con, "entities", args.entity)
            if args.project:
                _resolve(con, "projects", args.project)

    from expense_ledger import auto_link, set_meta

    ids, lines = [], []
    deltas: list[tuple[str, float]] = []
    for p in parsed:
        with redirect_stdout(io.StringIO()):
            record_id = add_expense(p.amount, p.type, p.category, p.item, p.remark, p.expense_time)
        linked = auto_link(record_id, p.item, args.entity, args.project, args.no_link)
        if linked["entity"] or linked["project"]:
            p.entity, p.project = linked["entity"], linked["project"]
        flow = args.flow or p.flow
        if flow or args.nature or args.original:
            set_meta(record_id, nature=args.nature, flow=flow, original=args.original)
        if flow in ("refund", "reimbursement"):
            deltas.append((p.category, -p.amount))
        elif p.type == "支出":
            deltas.append((p.category, p.amount))
        ids.append(record_id)
        lines.append(f"✅ 已记账：{_confirm_line(p, record_id)}")
        if flow in ("refund", "reimbursement") and not args.original:
            lines.append(f"   ℹ️ 未挂原单：只冲减本期支出，原单统计不变。"
                         f"要挂回去：meta {record_id} --flow {flow} --original <原单ID>")
        if linked["multi"]:
            lines.append(f"   ℹ️ 同时关联 {len(linked['entities'])} 个实体（全额计入各自名下），"
                         f"要按人分账请用 link --allocation")
        for w in p.warnings:
            lines.append(f"   ⚠️ {w}")
        if p.category == "其他":
            lines.append("   ⚠️ 分类未命中，已记入其他（可用 edit 修改）")

    if len(ids) > 1:
        total = sum(Decimal(str(p.amount)) for p in parsed)
        lines.append(f"   合计 ¥{_money(total)}（{len(ids)} 笔）")
    lines.extend(_budget_hint(deltas))

    print("\n".join(lines))
    if args.ids:
        print(",".join(str(i) for i in ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
