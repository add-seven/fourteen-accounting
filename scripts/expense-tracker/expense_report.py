#!/usr/bin/env python3
"""十四记账 · 确定性复盘报表：日报 / 周报 / 月报。

用法：
  python3 expense_report.py --day                      # 今天
  python3 expense_report.py --day --date 2026-11-05
  python3 expense_report.py --week                     # 本周（周一~周日）
  python3 expense_report.py 2026-11                    # 月报

设计原则：
- 三种报表共用同一套取数、口径与格式，金额一律走 net（已扣退款/报销到账）。
- 只陈述数据里的事实，不做“花多了/该省点”之类的判断。
- 没有信号的分段整段不出现，避免每天刷同样的提醒。
"""
import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from expense_ledger import (init, summary, budget_status, _rows,
                        _net_expenses, fmt, attribution, month_range, insight, cents)

WEEKDAY_CN = '一二三四五六日'


# ---------- 格式化 ----------

def money(value, sign=''):
    """11277.00 -> ¥11,277；25.5 -> ¥25.50。统一千分位，整数不带 .00。"""
    d = Decimal(str(value))
    body = f'{int(d):,}' if d == d.to_integral_value() else f'{d:,.2f}'
    return f'{sign}¥{body}'


def avg_money(total, days):
    """日均按元四舍五入，避免出现 ¥1,363.33 这种与其余金额不同粒度的数字。"""
    if not days:
        return money(0)
    return money((Decimal(str(total)) / days).quantize(Decimal('1'), ROUND_HALF_UP))


def pct(part, whole):
    """占比；有支出但不足 1% 时显示 <1%，不显示成误导性的 0%。"""
    if not whole or whole <= 0:
        return '—'
    ratio = Decimal(part) / Decimal(whole)
    if 0 < ratio < Decimal('0.01'):
        return '<1%'
    return f'{ratio:.0%}'


def _range_label(start_s, end_s):
    s = date.fromisoformat(start_s)
    e = date.fromisoformat(end_s) - timedelta(days=1)
    if s == e:
        return f'{s.isoformat()} 周{WEEKDAY_CN[s.weekday()]}'
    return f'{s.isoformat()} ~ {e.strftime("%m-%d")}'


# ---------- 取数 ----------

def _agg(start_s, end_s):
    rows = _rows(start_s, end_s)
    spend = [r for r in rows if r['flow_kind'] == 'expense' and r['type'] == '支出']
    # 收入只算真正的收入；退款/报销到账单独列，否则会被重复计一次
    income = [r for r in rows if r['flow_kind'] == 'income']
    refund = [r for r in rows if r['flow_kind'] in ('refund', 'reimbursement')]

    def yuan(rs):
        return Decimal(sum(cents(r['amount']) for r in rs)) / 100

    return {
        'rows': rows, 'spend': spend, 'income': income, 'refund': refund,
        'net': Decimal(_net_expenses(rows)) / 100,
        'gross': yuan(spend),
        'income_sum': yuan(income),
        'refund_sum': yuan(refund),
        'count': len(spend),
    }


def _elapsed_days(start_s, end_s, today=None):
    """区间内已过去的天数，用于日均；未来日期不计。"""
    today = today or date.today()
    s = date.fromisoformat(start_s)
    e = date.fromisoformat(end_s) - timedelta(days=1)
    last = min(e, today)
    if last < s:
        return 0
    return (last - s).days + 1


# ---------- 分段 ----------

def _budget_lines(period, note=''):
    data = budget_status(period)
    head = '💰 预算' + (f'（{note}）' if note else '')
    if not data:
        return [head, '未设置预算；可先只设月度总预算']
    data = sorted(data, key=lambda b: 0 if b['scope'] == 'total' else 1)
    lines = [head]
    for b in data:
        budget, spent, left = Decimal(b['budget']), Decimal(b['spent']), Decimal(b['remaining'])
        status = f'超 {money(-left)}' if left < 0 else f'剩 {money(left)}（已用 {pct(spent, budget)}）'
        lines.append(f"{b['name']}：{money(spent)} / {money(budget)}｜{status}")
    return lines


def _category_lines(start_s, end_s, limit=5):
    data = [x for x in summary(start_s, end_s, 'category') if x['name'] != '收入']
    if not data:
        return ['🏷️ 分类', '暂无支出']
    total = sum(Decimal(x['amount']) for x in data)
    lines = ['🏷️ 分类']
    for i, x in enumerate(data[:limit], 1):
        lines.append(f"{i}. {x['name']} {money(x['amount'])}"
                     f"（{x['transactions']} 笔，{pct(Decimal(x['amount']), total)}）")
    if len(data) > limit:
        rest = sum(Decimal(x['amount']) for x in data[limit:])
        lines.append(f"…另 {len(data) - limit} 类 {money(rest)}")
    return lines


def _entity_lines(start_s, end_s, limit=4):
    data = summary(start_s, end_s, 'entity', True)
    if not data:
        return []
    data = sorted(data, key=lambda x: -Decimal(x['amount']))
    lines = ['👤 实体（未标注 ≈ 自己）']
    shown = 0
    for x in data:
        name = '未标注' if x['name'].startswith('未归属') else x['name']
        if shown >= limit:
            break
        lines.append(f"{name} {money(x['amount'])}（{x['transactions']} 笔）")
        shown += 1
    return lines


def _project_lines(start_s, end_s, limit=4):
    data = summary(start_s, end_s, 'project')
    if not data:
        return []
    lines = ['📁 项目']
    for x in data[:limit]:
        lines.append(f"{x['name']} {money(x['amount'])}（{x['transactions']} 笔）")
    return lines


def _nature_lines(start_s, end_s):
    data = [x for x in summary(start_s, end_s, 'nature') if x['name'] != 'ordinary']
    if not data:
        return []
    names = {'planned_large': '计划内大额', 'unplanned_large': '计划外大额',
             'essential_large': '突发必要', 'unknown_large': '未分类大额'}
    lines = ['💸 大额（按性质）']
    for x in data:
        lines.append(f"{names.get(x['name'], x['name'])} {money(x['amount'])}（{x['transactions']} 笔）")
    return lines


def _diagnostics(start_s, end_s, period=None, max_lines=3):
    """只输出数据里确实存在的信号，宁缺勿滥。"""
    ins = insight(period, start=start_s, end=end_s)
    tips = []
    mom = ins['mom_percent']
    if mom is not None and abs(mom) >= 20:
        arrow = '↑' if mom > 0 else '↓'
        tips.append(f"{ins['mom_label']} {arrow}{abs(mom)}%"
                    f"（{money(ins['previous_total'])} → {money(ins['total'])}）")
    if ins['duplicates']:
        d = ins['duplicates'][0]
        tips.append(f"疑似重复：{d['item']} {money(d['amount'])} ×{d['count']}"
                    f"（ID {','.join(str(i) for i in d['ids'])}），属实可用 delete 去掉多记的")
    small = ins['small']
    share = small['share']
    if small['count'] >= 10 and share.endswith('%') and int(share.rstrip('%')) >= 10:
        tips.append(f"小额（≤{money(ins['small_threshold'])}）{small['count']} 笔，"
                    f"合计 {money(small['sum'])}，占 {share}")
    return tips[:max_lines]


# ---------- 三种报表 ----------

def day_report(anchor=None):
    init()
    anchor = anchor or date.today()
    if isinstance(anchor, str):
        anchor = date.fromisoformat(anchor)
    start_s = anchor.isoformat()
    end_s = (anchor + timedelta(days=1)).isoformat()
    a = _agg(start_s, end_s)
    period = anchor.strftime('%Y-%m')

    lines = [f'📊 {start_s} 周{WEEKDAY_CN[anchor.weekday()]} 日报', '']
    if not a['rows']:
        lines.append('今日暂无记录。')
        return '\n'.join(lines)
    lines.append(f"今日支出：{money(a['gross'])}｜{a['count']} 笔")
    if a['refund_sum']:
        lines.append(f"退款/报销到账：-{money(a['refund_sum'])} → 净支出 {money(a['net'])}")
    if a['income_sum']:
        lines.append(f"今日收入：+{money(a['income_sum'])}")

    bs = budget_status(period)
    total_budget = next((b for b in bs if b['scope'] == 'total'), None)
    if total_budget:
        left = Decimal(total_budget['remaining'])
        tail = f'超 {money(-left)}' if left < 0 else f'剩 {money(left)}'
        lines.append(f"本月累计：{money(total_budget['spent'])} / {money(total_budget['budget'])}｜{tail}")

    lines.append('')
    lines.extend(_category_lines(start_s, end_s, limit=4))
    lines.append('')
    lines.append('📋 明细')
    detail = a['spend'][:12]
    if not detail:
        lines.append('无支出')
    for r in detail:
        lines.append(f"· {r['expense_time'][11:16]} {r['item']} {money(r['amount'])}｜{r['category']}")
    if len(a['spend']) > len(detail):
        lines.append(f"…另 {len(a['spend']) - len(detail)} 笔")

    return '\n'.join(lines)


def week_report(anchor=None):
    init()
    anchor = anchor or date.today()
    if isinstance(anchor, str):
        anchor = date.fromisoformat(anchor)
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    start_s, end_s = monday.isoformat(), (sunday + timedelta(days=1)).isoformat()
    a = _agg(start_s, end_s)
    period = monday.strftime('%Y-%m')
    days = _elapsed_days(start_s, end_s) or 1

    lines = [f"📊 {_range_label(start_s, end_s)} 周报", '']
    if not a['rows']:
        lines.append('本周暂无记录。')
        return '\n'.join(lines)
    lines.append(f"本周支出：{money(a['gross'])}｜{a['count']} 笔｜日均 {avg_money(a['gross'], days)}")
    if a['refund_sum']:
        lines.append(f"退款/报销到账：-{money(a['refund_sum'])} → 净支出 {money(a['net'])}")
    if a['income_sum']:
        lines.append(f"本周收入：+{money(a['income_sum'])}")

    lines.append('')
    lines.extend(_budget_lines(period, '本月累计，含本周'))
    lines.append('')
    lines.extend(_category_lines(start_s, end_s))
    for section in (_entity_lines(start_s, end_s), _project_lines(start_s, end_s),
                    _nature_lines(start_s, end_s)):
        if section:
            lines.append('')
            lines.extend(section)

    tips = _diagnostics(start_s, end_s)
    if tips:
        lines.append('')
        lines.append('⚠️ 关注')
        lines.extend(tips)

    return '\n'.join(lines)


def month_report(period):
    init()
    start_s, end_s = month_range(period)
    a = _agg(start_s, end_s)
    days = _elapsed_days(start_s, end_s)
    total_days = (date.fromisoformat(end_s) - date.fromisoformat(start_s)).days

    lines = [f'📊 {period} 月报（1~{total_days} 日）', '']
    if not a['rows']:
        lines.append('本月暂无记录。')
        return '\n'.join(lines)
    daily = f"｜日均 {avg_money(a['gross'], days)}" if days else ''
    lines.append(f"支出：{money(a['gross'])}｜{a['count']} 笔{daily}")
    if a['refund_sum']:
        lines.append(f"退款/报销到账：-{money(a['refund_sum'])} → 净支出 {money(a['net'])}")
    if a['income_sum']:
        net_cash = a['income_sum'] - a['net']
        lines.append(f"收入：+{money(a['income_sum'])}｜净结余 "
                     f"{'-' if net_cash < 0 else '+'}{money(abs(net_cash))}")

    lines.append('')
    lines.extend(_budget_lines(period))
    lines.append('')
    lines.extend(_category_lines(start_s, end_s))

    for section in (_entity_lines(start_s, end_s), _project_lines(start_s, end_s),
                    _nature_lines(start_s, end_s)):
        if section:
            lines.append('')
            lines.extend(section)

    # 峰值日（前 3 天）
    per_day = {}
    for r in a['spend']:
        d = r['expense_time'][:10]
        per_day[d] = per_day.get(d, 0) + cents(r['amount'])
    top_days = sorted(per_day.items(), key=lambda x: -x[1])[:3]
    if top_days:
        lines.append('')
        lines.append('📅 支出最高的几天')
        lines.append('｜'.join(f"{d[5:]} {money(Decimal(v) / 100)}" for d, v in top_days))

    ins = insight(period)
    if ins['largest']:
        big = ins['largest']
        lines.append(f"📌 单笔最高：{big['item']} {money(big['amount'])}"
                     f"（{big['date'][5:]}，占 {big['share']}）")

    tips = _diagnostics(start_s, end_s, period)
    if tips:
        lines.append('')
        lines.append('⚠️ 关注')
        lines.extend(tips)

    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='十四记账 · 复盘报表')
    ap.add_argument('period', nargs='?', help='YYYY-MM（月报）')
    ap.add_argument('--day', action='store_true', help='日报（默认今天）')
    ap.add_argument('--week', action='store_true', help='周报（默认本周）')
    ap.add_argument('--date', help='日报/周报的锚点日期 YYYY-MM-DD')
    args = ap.parse_args()
    try:
        if args.day:
            print(day_report(args.date))
        elif args.week:
            print(week_report(args.date))
        elif args.period:
            print(month_report(args.period))
        else:
            ap.error('请选一种：--day / --week / YYYY-MM')
    except ValueError as e:
        print(f'❌ {e}')
        sys.exit(1)
