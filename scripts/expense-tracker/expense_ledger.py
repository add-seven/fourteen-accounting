#!/usr/bin/env python3
"""十四记账 · 账本管理：实体、项目、预算、流水查询与导出。

纯本地 SQLite，无网络与外部依赖。金额一律以「分」为整数存储，避免浮点误差。

命令分组：
- 初始化与维护  init / recat
- 实体与项目    create / link / archive / restore / objects
- 预算          budget-set / budget-copy / budget-show
- 流水          summary / review / transactions / insight / export
- 单笔修正      edit / delete / meta
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / 'expenses.db'
DB_PATH = Path(os.environ.get('EXPENSE_DB_PATH') or DEFAULT_DB).expanduser()


def cents(value):
    v = Decimal(str(value))
    if not v.is_finite() or v <= 0:
        raise ValueError('金额必须大于0且为有限数')
    return int((v * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def fmt(value):
    return f'{Decimal(value) / 100:.2f}'


def money(value):
    """统一展示格式：整数不带小数，其余保留两位并加千分位。"""
    d = Decimal(str(value))
    return f'{int(d):,}' if d == d.to_integral_value() else f'{d:,.2f}'


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=5000')
    return con


def month_range(period):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', str(period)):
        raise ValueError(f'月份格式应为 YYYY-MM（如 2026-11），收到：{period}')
    y, m = map(int, period.split('-'))
    start = f'{period}-01'
    end = f'{y + 1}-01-01' if m == 12 else f'{y}-{m + 1:02d}-01'
    return start, end


def _adopt_legacy_tables(con):
    """早期账本文件用带前缀的表名；检测到就改名并入，避免实体与预算读不到。"""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    prefix = 'v2_'
    for suffix in ('entities', 'projects', 'transaction_entities',
                   'transaction_projects', 'transaction_meta', 'budgets'):
        old, new = prefix + suffix, suffix
        if old in have and new not in have:
            con.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')


def init(backup=False):
    # Preserve the original SQLite REAL amount and existing integrations.
    from expense_sync import init_db
    if backup and DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        target = DB_PATH.with_name(f'{DB_PATH.name}.bak-{stamp}')
        shutil.copy2(DB_PATH, target)
    init_db()
    with connect() as con:
        _adopt_legacy_tables(con)
        con.executescript('''
        CREATE TABLE IF NOT EXISTS entities(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL CHECK(kind IN ('person','asset','group','other')),
          aliases TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          aliases TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active'
             CHECK(status IN ('active','completed','archived')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS transaction_entities(
          transaction_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
          entity_id INTEGER NOT NULL REFERENCES entities(id),
          allocated_cents INTEGER CHECK(allocated_cents IS NULL OR allocated_cents >= 0),
          PRIMARY KEY(transaction_id, entity_id));
        CREATE TABLE IF NOT EXISTS transaction_projects(
          transaction_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
          project_id INTEGER NOT NULL REFERENCES projects(id),
          PRIMARY KEY(transaction_id, project_id));
        CREATE TABLE IF NOT EXISTS transaction_meta(
          transaction_id INTEGER PRIMARY KEY REFERENCES expenses(id) ON DELETE CASCADE,
          category_detail TEXT, nature TEXT NOT NULL DEFAULT 'ordinary'
            CHECK(nature IN ('ordinary','planned_large','unplanned_large','essential_large','unknown_large')),
          flow_kind TEXT NOT NULL DEFAULT 'expense'
            CHECK(flow_kind IN ('expense','income','transfer','refund','reimbursement')),
          original_transaction_id INTEGER REFERENCES expenses(id),
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS budgets(
          id INTEGER PRIMARY KEY, period TEXT NOT NULL, scope TEXT NOT NULL
            CHECK(scope IN ('total','category','project')),
          scope_ref TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
          coverage TEXT NOT NULL DEFAULT 'all' CHECK(coverage IN ('all','ordinary')),
          UNIQUE(period, scope, scope_ref));
        CREATE INDEX IF NOT EXISTS idx_entity_tx ON transaction_entities(entity_id);
        CREATE INDEX IF NOT EXISTS idx_project_tx ON transaction_projects(project_id);
        ''')


def _aliases(s):
    return [x.strip() for x in re.split(r'[,，、]', s or '') if x.strip()]


def create(kind, name, aliases=''):
    init()
    table = 'entities' if kind != 'project' else 'projects'
    if kind != 'project' and kind not in ('person', 'asset', 'group', 'other'):
        raise ValueError('kind应为person/asset/group/other/project')
    alias_list = _aliases(aliases)
    short = [a for a in alias_list if len(a) < 2]
    if short:
        raise ValueError(f'别名至少 2 个字：{",".join(short)}（单字别名会误命中，如“我”命中“自我提升”）')
    with connect() as con:
        exists = con.execute(f'SELECT id,aliases FROM {table} WHERE name=?', (name,)).fetchone()
        if exists:
            merged = sorted(set(json.loads(exists['aliases']) + _aliases(aliases)))
            con.execute(f'UPDATE {table} SET aliases=? WHERE id=?',
                        (json.dumps(merged, ensure_ascii=False), exists['id']))
            return {'name': name, 'status': '已存在，别名已合并', 'aliases': merged}
        if kind == 'project':
            con.execute(f'INSERT INTO {table}(name,aliases) VALUES(?,?)',
                        (name, json.dumps(_aliases(aliases), ensure_ascii=False)))
        else:
            con.execute(f'INSERT INTO {table}(name,kind,aliases) VALUES(?,?,?)',
                        (name, kind, json.dumps(_aliases(aliases), ensure_ascii=False)))
    return {'name': name, 'status': 'created', 'aliases': _aliases(aliases)}


def match(text, table):
    """Longest unambiguous alias/name match; no silent guessing on ties."""
    with connect() as con:
        rows = con.execute(
            f'SELECT * FROM {table} WHERE ' + ("status='active'" if table == 'projects' else 'active=1')
        ).fetchall()
    scored = []
    for r in rows:
        terms = [r['name']] + [a for a in json.loads(r['aliases']) if len(a) >= 2]
        found = [len(t) for t in terms if t and t.lower() in text.lower()]
        if found:
            scored.append((max(found), r['id'], r['name']))
    if not scored:
        return None
    best = max(x[0] for x in scored)
    candidates = [x for x in scored if x[0] == best]
    return candidates[0] if len(candidates) == 1 else None


def match_all(text, table):
    """列出文本里所有明确命中的实体/项目（按出现顺序）。

    规则：最长命中优先；被更长命中覆盖的短命中丢弃（“张三”不会在“张三丰”里再命中）；
    同长度且区间重叠的不同对象视为歧义，两个都不返回。
    """
    with connect() as con:
        rows = con.execute(
            f'SELECT * FROM {table} WHERE ' + ("status='active'" if table == 'projects' else 'active=1')
        ).fetchall()
    hits = []
    low = text.lower()
    for r in rows:
        terms = [r['name']] + [a for a in json.loads(r['aliases']) if len(a) >= 2]
        best = None
        for t in terms:
            idx = low.find(t.lower())
            if idx >= 0 and (best is None or len(t) > best[0]):
                best = (len(t), idx, idx + len(t))
        if best:
            hits.append((best[0], best[1], best[2], r['name']))

    ambiguous = set()
    for i, a in enumerate(hits):
        for b in hits[i + 1:]:
            if a[3] == b[3]:
                continue
            overlap = not (a[2] <= b[1] or b[2] <= a[1])
            if a[0] == b[0] and overlap:
                ambiguous.update([a[3], b[3]])
    kept = []
    for h in hits:
        if h[3] in ambiguous:
            continue
        if any(g[0] > h[0] and g[1] <= h[1] and g[2] >= h[2] for g in hits):
            continue  # 被更长的命中完整覆盖
        kept.append(h)
    return [h[3] for h in sorted(kept, key=lambda x: (x[1], -x[0]))]


def _resolve(con, table, value):
    r = con.execute(f'SELECT id FROM {table} WHERE name=?', (value,)).fetchone()
    if not r:
        raise ValueError(f'未找到已创建的对象：{value}，请先 create')
    return r['id']


def link(tx_id, entity=None, project=None, allocation=None, replace=False):
    init()
    with connect() as con:
        expense = con.execute('SELECT id,amount,type FROM expenses WHERE id=?', (tx_id,)).fetchone()
        if not expense:
            raise ValueError(f'交易ID不存在：{tx_id}')
        if replace:
            if entity is not None:
                con.execute('DELETE FROM transaction_entities WHERE transaction_id=?', (tx_id,))
            if project is not None:
                con.execute('DELETE FROM transaction_projects WHERE transaction_id=?', (tx_id,))
        if entity:
            eid = _resolve(con, 'entities', entity)
            alloc = cents(allocation) if allocation is not None else None
            if alloc is not None and alloc > cents(expense['amount']):
                raise ValueError('分摊金额不能超过交易金额')
            con.execute('INSERT INTO transaction_entities(transaction_id,entity_id,allocated_cents) '
                        'VALUES(?,?,?) ON CONFLICT(transaction_id,entity_id) '
                        'DO UPDATE SET allocated_cents=excluded.allocated_cents', (tx_id, eid, alloc))
            allocated = con.execute(
                'SELECT COALESCE(SUM(allocated_cents),0) AS total FROM transaction_entities '
                'WHERE transaction_id=?', (tx_id,)).fetchone()['total']
            if allocated > cents(expense['amount']):
                raise ValueError('所有实体分摊合计不能超过实际金额')
        if project:
            pid = _resolve(con, 'projects', project)
            con.execute('INSERT OR IGNORE INTO transaction_projects(transaction_id,project_id) '
                        'VALUES(?,?)', (tx_id, pid))


def auto_link(tx_id, text, entity=None, project=None):
    """只认显式名称/别名，不做人物推断；一句话里的多个明确实体都会关联。

    多实体不带分摊时按“关联金额”记录（同一笔会在多个实体下各显示全额），
    报表口径已注明不可相加；需要按人算就用 link --allocation。
    """
    init()
    names = [entity] if entity else match_all(text, 'entities')
    p = project or (m[2] if (m := match(text, 'projects')) else None)
    for n in names:
        link(tx_id, n, p)
    return {'entity': '、'.join(names) if names else None,
            'entities': names, 'project': p,
            'multi': len(names) > 1}


def archive(name, kind='entity', restore=False):
    """归档/恢复实体或项目。归档后不再参与文本自动匹配，历史账不受影响。"""
    init()
    table = 'projects' if kind == 'project' else 'entities'
    with connect() as con:
        row = con.execute(f'SELECT * FROM {table} WHERE name=?', (name,)).fetchone()
        if not row:
            raise ValueError(f'未找到{"项目" if kind == "project" else "实体"}：{name}')
        if kind == 'project':
            con.execute('UPDATE projects SET status=? WHERE id=?',
                        ('active' if restore else 'archived', row['id']))
            linked = con.execute('SELECT COUNT(*) FROM transaction_projects WHERE project_id=?',
                                 (row['id'],)).fetchone()[0]
        else:
            con.execute('UPDATE entities SET active=? WHERE id=?',
                        (1 if restore else 0, row['id']))
            linked = con.execute('SELECT COUNT(*) FROM transaction_entities WHERE entity_id=?',
                                 (row['id'],)).fetchone()[0]
    return {'name': name, 'kind': kind, 'status': 'active' if restore else 'archived',
            'linked_transactions': linked,
            'note': '历史账保留；归档后不再被文本自动匹配'}


def objects(kind=None):
    """列出实体/项目及其状态与使用情况，用于定期清理。"""
    init()
    out = {'entities': [], 'projects': []}
    with connect() as con:
        if kind in (None, 'entity'):
            for r in con.execute('''SELECT e.id,e.name,e.kind,e.active,e.aliases,
                  (SELECT COUNT(*) FROM transaction_entities t WHERE t.entity_id=e.id) cnt,
                  (SELECT MAX(x.expense_time) FROM transaction_entities t
                    JOIN expenses x ON x.id=t.transaction_id WHERE t.entity_id=e.id) last_used
                  FROM entities e ORDER BY e.active DESC,e.kind,e.name'''):
                out['entities'].append({'name': r['name'], 'kind': r['kind'],
                                        'status': 'active' if r['active'] else 'archived',
                                        'aliases': json.loads(r['aliases']),
                                        'transactions': r['cnt'],
                                        'last_used': (r['last_used'] or '')[:10]})
        if kind in (None, 'project'):
            for r in con.execute('''SELECT p.id,p.name,p.status,p.aliases,
                  (SELECT COUNT(*) FROM transaction_projects t WHERE t.project_id=p.id) cnt,
                  (SELECT MAX(x.expense_time) FROM transaction_projects t
                    JOIN expenses x ON x.id=t.transaction_id WHERE t.project_id=p.id) last_used
                  FROM projects p ORDER BY p.status,p.name'''):
                out['projects'].append({'name': r['name'], 'status': r['status'],
                                        'aliases': json.loads(r['aliases']),
                                        'transactions': r['cnt'],
                                        'last_used': (r['last_used'] or '')[:10]})
    if kind == 'entity':
        return out['entities']
    if kind == 'project':
        return out['projects']
    return out



def set_meta(tx_id, nature=None, flow=None, detail=None, original=None):
    init()
    with connect() as con:
        if not con.execute('SELECT 1 FROM expenses WHERE id=?', (tx_id,)).fetchone():
            raise ValueError(f'交易ID不存在：{tx_id}')
        if original is not None and not con.execute(
                'SELECT 1 FROM expenses WHERE id=?', (original,)).fetchone():
            raise ValueError(f'原交易ID不存在：{original}')
        con.execute('INSERT OR IGNORE INTO transaction_meta(transaction_id) VALUES(?)', (tx_id,))
        if nature:
            con.execute('UPDATE transaction_meta SET nature=?,updated_at=CURRENT_TIMESTAMP '
                        'WHERE transaction_id=?', (nature, tx_id))
        if flow:
            con.execute('UPDATE transaction_meta SET flow_kind=?,original_transaction_id=?,'
                        'updated_at=CURRENT_TIMESTAMP WHERE transaction_id=?', (flow, original, tx_id))
        if detail:
            con.execute('UPDATE transaction_meta SET category_detail=?,updated_at=CURRENT_TIMESTAMP '
                        'WHERE transaction_id=?', (detail, tx_id))


def edit(tx_id, amount=None, category=None, item=None, date=None, type_=None,
         remark=None, nature=None, flow=None):
    """修正历史账：一条命令搞定，不用手写 SQL。返回修改前后差异。"""
    init()
    if date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?', date):
        raise ValueError('date格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM')
    with connect() as con:
        row = con.execute('SELECT * FROM expenses WHERE id=?', (tx_id,)).fetchone()
        if not row:
            raise ValueError(f'交易ID不存在：{tx_id}')
        before = dict(row)
        if amount is not None:
            alloc = con.execute('SELECT COALESCE(SUM(allocated_cents),0) FROM transaction_entities '
                                'WHERE transaction_id=?', (tx_id,)).fetchone()[0]
            if alloc > cents(amount):
                raise ValueError(f'新金额 {amount} 低于已分摊合计 {fmt(alloc)}，'
                                 f'请先用 link --allocation 调整分摊')
        fields, values = [], []
        if amount is not None:
            fields.append('amount=?')
            values.append(float(Decimal(str(amount))))
        if category:
            fields.append('category=?')
            values.append(category)
        if item:
            fields.append('item=?')
            values.append(item)
        if type_:
            if type_ not in ('支出', '收入'):
                raise ValueError('type只能是 支出/收入')
            fields.append('type=?')
            values.append(type_)
        if remark is not None:
            fields.append('remark=?')
            values.append(remark)
        if date:
            from expense_sync import normalize_time
            fields.append('expense_time=?')
            values.append(normalize_time(date))
        if fields:
            values.append(tx_id)
            con.execute(f'UPDATE expenses SET {",".join(fields)} WHERE id=?', tuple(values))
    if nature or flow:
        set_meta(tx_id, nature=nature, flow=flow)
    with connect() as con:
        after = dict(con.execute('SELECT * FROM expenses WHERE id=?', (tx_id,)).fetchone())
    changed = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
    return {'id': tx_id, 'changed': changed}


def delete(tx_id, confirm=False):
    """删除一笔账（含实体/项目关联）。默认只预览，必须 --yes 才真删。"""
    init()
    with connect() as con:
        row = con.execute('SELECT * FROM expenses WHERE id=?', (tx_id,)).fetchone()
        if not row:
            raise ValueError(f'交易ID不存在：{tx_id}')
        info = {'id': tx_id, 'amount': row['amount'], 'item': row['item'],
                'date': row['expense_time']}
        if not confirm:
            return {'would_delete': info, 'note': '确认无误后加 --yes 再执行'}
        con.execute('DELETE FROM expenses WHERE id=?', (tx_id,))
    with connect() as con:
        orphan = con.execute('SELECT COUNT(*) FROM transaction_entities WHERE transaction_id=?',
                             (tx_id,)).fetchone()[0]
        orphan += con.execute('SELECT COUNT(*) FROM transaction_projects WHERE transaction_id=?',
                              (tx_id,)).fetchone()[0]
    return {'deleted': info, 'orphan_links': orphan}


def set_budget(period, scope, amount, ref='', coverage='all'):
    init()
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        raise ValueError('period格式: YYYY-MM')
    if scope == 'total':
        ref = ''
    elif not ref:
        raise ValueError('分类预算/项目预算必须指定名称')
    if scope == 'project':
        with connect() as con:
            _resolve(con, 'projects', ref)
    elif scope == 'category':
        from expense_sync import CATEGORIES
        with connect() as con:
            used = {r[0] for r in con.execute('SELECT DISTINCT category FROM expenses')}
        known = set(CATEGORIES) | used
        if ref not in known:
            raise ValueError(f'未知分类「{ref}」，可用：' + '、'.join(sorted(known)))
    with connect() as con:
        con.execute('INSERT INTO budgets(period,scope,scope_ref,amount_cents,coverage) '
                    'VALUES(?,?,?,?,?) ON CONFLICT(period,scope,scope_ref) '
                    'DO UPDATE SET amount_cents=excluded.amount_cents,coverage=excluded.coverage',
                    (period, scope, ref, cents(amount), coverage))


def copy_budget(period, source):
    """把上个月的预算整体继承过来，避免每月手工重设。"""
    init()
    if period == source:
        raise ValueError('目标月份与来源月份不能相同')
    with connect() as con:
        rows = con.execute('SELECT * FROM budgets WHERE period=?', (source,)).fetchall()
        if not rows:
            raise ValueError(f'{source} 没有已配置的预算')
        existing = {(r['scope'], r['scope_ref']) for r in
                    con.execute('SELECT scope,scope_ref FROM budgets WHERE period=?', (period,))}
        overwritten = 0
        for r in rows:
            if (r['scope'], r['scope_ref']) in existing:
                overwritten += 1
            con.execute('INSERT INTO budgets(period,scope,scope_ref,amount_cents,coverage) '
                        'VALUES(?,?,?,?,?) ON CONFLICT(period,scope,scope_ref) '
                        'DO UPDATE SET amount_cents=excluded.amount_cents,coverage=excluded.coverage',
                        (period, r['scope'], r['scope_ref'], r['amount_cents'], r['coverage']))
    return {'period': period, 'from': source, 'copied': len(rows), 'overwritten': overwritten}


def _rows(start, end):
    with connect() as con:
        return con.execute('''SELECT x.*,COALESCE(m.flow_kind,CASE WHEN x.type='收入' THEN 'income' ELSE 'expense' END) flow_kind,
          COALESCE(m.nature,'ordinary') nature, m.original_transaction_id
          FROM expenses x LEFT JOIN transaction_meta m ON m.transaction_id=x.id
          WHERE expense_time>=? AND expense_time<? ORDER BY expense_time,id''', (start, end)).fetchall()


def _net_expenses(rows, coverage='all'):
    """净支出 = 支出 - 退款/报销到账。退款即使未关联原单也冲减（钱确实回来了）。"""
    total = 0
    for r in rows:
        if coverage == 'ordinary' and r['nature'] != 'ordinary':
            continue
        amount = cents(r['amount'])
        if r['flow_kind'] == 'expense' and r['type'] == '支出':
            total += amount
        elif r['flow_kind'] in ('refund', 'reimbursement'):
            total -= amount
    return total


def budget_status(period):
    init()
    start, end = month_range(period)
    rows = _rows(start, end)
    with connect() as con:
        budgets = con.execute('SELECT * FROM budgets WHERE period=? ORDER BY scope,scope_ref',
                              (period,)).fetchall()
        result = []
        for b in budgets:
            scoped = rows
            if b['scope'] == 'category':
                scoped = [r for r in rows if r['category'] == b['scope_ref']]
            elif b['scope'] == 'project':
                ids = {r['transaction_id'] for r in con.execute(
                    '''SELECT tp.transaction_id FROM transaction_projects tp
                       JOIN projects p ON p.id=tp.project_id WHERE p.name=?''', (b['scope_ref'],))}
                scoped = [r for r in rows if r['id'] in ids]
            spent = _net_expenses(scoped, b['coverage'])
            result.append({'scope': b['scope'], 'name': b['scope_ref'] or '月度总预算',
                           'coverage': b['coverage'], 'budget': fmt(b['amount_cents']),
                           'spent': fmt(spent), 'remaining': fmt(b['amount_cents'] - spent)})
    return result


def _entity_rows(con, start, end):
    """实体维度原始行，带分摊信息；alloc_count>0 表示该笔已明确分摊。"""
    return con.execute('''SELECT x.id,x.amount,x.type,t.name label,te.allocated_cents alloc,
      (SELECT COUNT(*) FROM transaction_entities z
        WHERE z.transaction_id=x.id AND z.allocated_cents IS NOT NULL) alloc_count,
      COALESCE(m.flow_kind,CASE WHEN x.type='收入' THEN 'income' ELSE 'expense' END) flow
      FROM expenses x JOIN transaction_entities te ON te.transaction_id=x.id
      JOIN entities t ON t.id=te.entity_id
      LEFT JOIN transaction_meta m ON m.transaction_id=x.id
      WHERE x.expense_time>=? AND x.expense_time<?''', (start, end)).fetchall()


def attribution(start, end):
    """已归属/未归属金额（每笔只计一次），用于回答“我自己花了多少”。"""
    rows = _rows(start, end)
    con = connect()
    try:
        per_tx = {}
        for l in con.execute('''SELECT te.transaction_id, te.allocated_cents,
              (SELECT COUNT(*) FROM transaction_entities z
                WHERE z.transaction_id=te.transaction_id AND z.allocated_cents IS NOT NULL) alloc_count
              FROM transaction_entities te'''):
            if l['alloc_count']:
                per_tx[l['transaction_id']] = per_tx.get(l['transaction_id'], 0) + (l['allocated_cents'] or 0)
            else:
                per_tx[l['transaction_id']] = None
    finally:
        con.close()
    attributed = 0
    counted = 0
    unattributed_count = 0
    for r in rows:
        if not (r['flow_kind'] == 'expense' and r['type'] == '支出'):
            continue
        if r['id'] in per_tx:
            counted += 1
            share = per_tx[r['id']]
            attributed += cents(r['amount']) if share is None else share
        else:
            unattributed_count += 1
    return {'attributed': attributed, 'unattributed': _net_expenses(rows) - attributed,
            'attributed_count': counted, 'unattributed_count': unattributed_count}


def summary(start, end, group='category', include_unattributed=False):
    init()
    if end <= start:
        raise ValueError('end必须晚于start（不含）')
    con = connect()
    try:
        if group == 'category':
            records = con.execute('''SELECT x.id,x.amount,x.type,x.category label,
              COALESCE(m.flow_kind,CASE WHEN x.type='收入' THEN 'income' ELSE 'expense' END) flow
              FROM expenses x LEFT JOIN transaction_meta m ON m.transaction_id=x.id
              WHERE x.expense_time>=? AND x.expense_time<?''', (start, end)).fetchall()
        elif group == 'nature':
            records = con.execute('''SELECT x.id,x.amount,x.type,COALESCE(m.nature,'ordinary') label,
              COALESCE(m.flow_kind,CASE WHEN x.type='收入' THEN 'income' ELSE 'expense' END) flow
              FROM expenses x LEFT JOIN transaction_meta m ON m.transaction_id=x.id
              WHERE x.expense_time>=? AND x.expense_time<?''', (start, end)).fetchall()
        elif group == 'entity':
            records = _entity_rows(con, start, end)
        elif group == 'project':
            records = con.execute('''SELECT x.id,x.amount,x.type,t.name label,
              COALESCE(m.flow_kind,CASE WHEN x.type='收入' THEN 'income' ELSE 'expense' END) flow
              FROM expenses x JOIN transaction_projects tp ON tp.transaction_id=x.id
              JOIN projects t ON t.id=tp.project_id
              LEFT JOIN transaction_meta m ON m.transaction_id=x.id
              WHERE x.expense_time>=? AND x.expense_time<?''', (start, end)).fetchall()
        else:
            raise ValueError('group仅支持category/entity/project/nature')
        records = [dict(r) for r in records]
    finally:
        con.close()

    amounts, ids = {}, {}
    for r in records:
        flow = r['flow']
        if flow == 'expense' and r['type'] == '支出':
            sign = 1
        elif flow in ('refund', 'reimbursement'):
            sign = -1
        else:
            continue
        # 分类/性质榜只排支出明细；退款不是一个“分类”。实体/项目维度仍按净额冲减。
        if group in ('category', 'nature') and flow != 'expense':
            continue
        if group == 'entity':
            if r.get('alloc_count'):
                share = r.get('alloc') or 0
                if share == 0:
                    continue  # 已明确分摊，但没分给这个实体
                base = share
            else:
                base = cents(r['amount'])
        else:
            base = cents(r['amount'])
        amounts[r['label']] = amounts.get(r['label'], 0) + sign * base
        ids.setdefault(r['label'], set()).add(r['id'])

    out = [{'name': k, 'amount': fmt(v), 'transactions': len(ids[k])}
           for k, v in sorted(amounts.items(), key=lambda a: -a[1])]

    if include_unattributed and group == 'entity':
        a = attribution(start, end)
        if a['unattributed_count'] or a['unattributed']:
            out.append({'name': '未归属（可视为自己/其他）', 'amount': fmt(a['unattributed']),
                        'transactions': a['unattributed_count']})
    return out


def advisory(period):
    """Deterministic advisory based on observed data; no fabricated causal stories."""
    budgets = budget_status(period)
    alerts = []
    for b in budgets:
        limit = Decimal(b['budget'])
        spent = Decimal(b['spent'])
        if spent > limit:
            alerts.append(f"{b['name']} 超支 {money(spent - limit)} 元")
        elif limit and spent / limit >= Decimal('.8'):
            alerts.append(f"{b['name']} 已使用 {spent / limit:.0%}")
    return alerts


def review(period):
    start, end = month_range(period)
    attr = attribution(start, end)
    natures = [x for x in summary(start, end, 'nature') if x['name'] != 'ordinary']
    return {
        'budget': budget_status(period),
        'alerts': advisory(period),
        'attribution': {'attributed': fmt(attr['attributed']),
                        'unattributed': fmt(attr['unattributed'])},
        'large_by_nature': natures,
        'top_entities': summary(start, end, 'entity', include_unattributed=True)[:8],
    }


def prev_period(period):
    y, m = map(int, str(period).split('-'))
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f'{y}-{m:02d}'


def _link_maps(con):
    ent, proj = {}, {}
    for r in con.execute('''SELECT te.transaction_id, e.name, te.allocated_cents
          FROM transaction_entities te JOIN entities e ON e.id=te.entity_id'''):
        ent.setdefault(r['transaction_id'], []).append(
            {'name': r['name'],
             'allocation': fmt(r['allocated_cents']) if r['allocated_cents'] is not None else None})
    for r in con.execute('''SELECT tp.transaction_id, p.name
          FROM transaction_projects tp JOIN projects p ON p.id=tp.project_id'''):
        proj.setdefault(r['transaction_id'], []).append(r['name'])
    return ent, proj


def transactions(start, end, entity=None, project=None, category=None,
                 unassigned=False, limit=50):
    """按条件查流水，最新在前；unassigned 只列没有实体关联的支出。"""
    init()
    if end <= start:
        raise ValueError('end必须晚于start（不含）')
    rows = list(_rows(start, end))
    con = connect()
    try:
        ent, proj = _link_maps(con)
    finally:
        con.close()

    out = []
    for r in sorted(rows, key=lambda x: (x['expense_time'], x['id']), reverse=True):
        e = ent.get(r['id'], [])
        p = proj.get(r['id'], [])
        if entity and not any(x['name'] == entity for x in e):
            continue
        if project and project not in p:
            continue
        if category and r['category'] != category:
            continue
        if unassigned and (e or not (r['flow_kind'] == 'expense' and r['type'] == '支出')):
            continue
        out.append({'id': r['id'], 'date': r['expense_time'][:10],
                    'amount': fmt(cents(r['amount'])), 'type': r['type'],
                    'category': r['category'], 'item': r['item'],
                    'entities': '、'.join(x['name'] for x in e),
                    'project': '、'.join(p), 'flow': r['flow_kind'],
                    'nature': r['nature'], 'remark': r['remark'] or ''})
        if limit and len(out) >= limit:
            break
    return out


def export_csv(out_path, start='0000-01-01', end='9999-12-31'):
    """导出为 UTF-8 BOM 的 CSV，Excel/WPS 直接打开不乱码。"""
    init()
    rows = sorted(_rows(start, end), key=lambda x: x['id'])
    con = connect()
    try:
        ent, proj = _link_maps(con)
    finally:
        con.close()

    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['ID', '日期', '金额', '收支', '分类', '事项', '备注',
                    '资金流', '性质', '实体', '分摊', '项目'])
        for r in rows:
            e = ent.get(r['id'], [])
            w.writerow([r['id'], r['expense_time'][:10], f"{Decimal(str(r['amount'])):.2f}",
                        r['type'], r['category'], r['item'], r['remark'] or '',
                        r['flow_kind'], r['nature'],
                        '、'.join(x['name'] for x in e),
                        '、'.join(f"{x['name']}:{x['allocation']}" for x in e if x['allocation']),
                        '、'.join(proj.get(r['id'], []))])
    return {'path': str(path), 'rows': len(rows), 'range': [start, end]}


def insight(period=None, small=None, dup_days=0, top=3, start=None, end=None):
    """确定性消费诊断：环比、分类涨跌、小额高频、疑似重复、最大单笔。

    不传 start/end 时按整月（period 为 YYYY-MM）；传了则按该区间，环比基准是等长的上一个区间。
    只陈述数据里已存在的事实，不做因果解释；阈值可调，不写死。
    """
    init()
    if start and end:
        s = date.fromisoformat(str(start)[:10])
        e = date.fromisoformat(str(end)[:10])
        span = (e - s).days
        if span <= 0:
            raise ValueError('end必须晚于start（不含）')
        prev_start = (s - timedelta(days=span)).isoformat()
        prev_end = s.isoformat()
        prev_label = f"{prev_start}~{(e - timedelta(days=span)).isoformat()}"
        start, end = s.isoformat(), e.isoformat()
        label = period or f'{start}~{end}'
        mom_label = '环比上周' if span == 7 else ('环比上月' if 28 <= span <= 31 else '环比上一周期')
    else:
        start, end = month_range(period)
        prev_label = prev_period(period)
        mom_label = '环比上月'
        prev_start, prev_end = month_range(prev_label)
        label = period
        mom_label = '环比上月'

    limit_cents = int((Decimal(str(small)) if small is not None else Decimal('50')) * 100)
    all_rows = _rows(start, end)
    rows = [r for r in all_rows if r['flow_kind'] == 'expense' and r['type'] == '支出']
    # 口径必须与报表一致：净支出（已扣退款/报销到账），否则同一份报表会出现两个总数
    total = _net_expenses(all_rows)
    gross = sum(cents(r['amount']) for r in rows)
    prev_total = _net_expenses(_rows(prev_start, prev_end))

    mom = None
    if prev_total > 0:
        mom = float((Decimal(total - prev_total) / Decimal(prev_total) * 100)
                    .quantize(Decimal('0.1')))

    cur_c, prev_c = {}, {}
    for r in rows:
        cur_c[r['category']] = cur_c.get(r['category'], 0) + cents(r['amount'])
    for r in _rows(prev_start, prev_end):
        if r['flow_kind'] == 'expense' and r['type'] == '支出':
            prev_c[r['category']] = prev_c.get(r['category'], 0) + cents(r['amount'])
    moves = [{'category': k, 'current': fmt(v), 'previous': fmt(prev_c.get(k, 0)),
              'delta': fmt(v - prev_c.get(k, 0))} for k, v in cur_c.items()]
    moves.sort(key=lambda x: -Decimal(x['delta']))

    small_rows = [r for r in rows if cents(r['amount']) <= limit_cents]
    small_sum = sum(cents(r['amount']) for r in small_rows)

    items = {}
    for r in rows:
        s = items.setdefault(r['item'], {'count': 0, 'sum': 0})
        s['count'] += 1
        s['sum'] += cents(r['amount'])
    frequent = [{'item': k, 'count': v['count'], 'sum': fmt(v['sum']),
                 'avg': fmt(v['sum'] // v['count'])}
                for k, v in items.items() if v['count'] >= 5]
    frequent.sort(key=lambda x: (-x['count'], -Decimal(x['sum'])))

    groups = {}
    for r in rows:
        groups.setdefault((r['item'], cents(r['amount'])), []).append(r)
    duplicates = []
    for (item, amt), rs in groups.items():
        if len(rs) < 2:
            continue
        dates = sorted(r['expense_time'][:10] for r in rs)
        if (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days <= dup_days:
            duplicates.append({'item': item, 'amount': fmt(amt), 'count': len(rs),
                               'ids': sorted(r['id'] for r in rs), 'dates': dates})
    duplicates.sort(key=lambda x: (-x['count'], -Decimal(x['amount'])))

    largest = None
    if rows:
        big = max(rows, key=lambda r: r['amount'])
        largest = {'id': big['id'], 'item': big['item'], 'amount': fmt(cents(big['amount'])),
                   'date': big['expense_time'][:10],
                   'share': f'{cents(big["amount"]) / total:.0%}' if total > 0 else '—'}

    return {'period': label, 'range': [start, end], 'total': fmt(total),
            'previous_period': prev_label, 'previous_total': fmt(prev_total), 'mom_percent': mom,
            'mom_label': mom_label,
            'category_growth': [m for m in moves if Decimal(m['delta']) > 0][:top],
            'category_shrink': [m for m in moves if Decimal(m['delta']) < 0][-top:],
            'small_threshold': fmt(limit_cents),
            'small': {'count': len(small_rows), 'sum': fmt(small_sum),
                      'share': f'{small_sum / total:.0%}' if total > 0 else '—',
                      'gross_sum': fmt(gross)},
            'frequent_items': frequent[:top], 'duplicates': duplicates[:top],
            'largest': largest}


def recategorize(apply=False, only_other=True):
    """按当前词表回填分类；默认只改“其他”，先预览后执行。"""
    from expense_sync import auto_categorize
    init()
    with connect() as con:
        rows = con.execute("SELECT id,item,category,expense_time FROM expenses "
                           "WHERE type='支出' ORDER BY expense_time,id").fetchall()
        changes = []
        for r in rows:
            if only_other and r['category'] not in ('其他', ''):
                continue
            new = auto_categorize(r['item'])
            if new != r['category']:
                changes.append({'id': r['id'], 'date': r['expense_time'][:10],
                                'item': r['item'], 'from': r['category'], 'to': new})
        if apply:
            for c in changes:
                con.execute('UPDATE expenses SET category=? WHERE id=?', (c['to'], c['id']))
    return {'apply': apply, 'count': len(changes), 'changes': changes[:50],
            'truncated': len(changes) > 50}


def cli():
    ap = argparse.ArgumentParser(description='十四记账 · 账本管理')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='幂等建库，已存在则只补齐缺失结构')
    p.add_argument('--backup', action='store_true', help='初始化前先备份数据库')
    p = sub.add_parser('create', help='创建实体/项目（重名则合并别名）')
    p.add_argument('kind', choices=['person', 'asset', 'group', 'other', 'project'])
    p.add_argument('name')
    p.add_argument('--aliases', default='')
    p = sub.add_parser('link', help='把已有账单关联实体/项目')
    p.add_argument('id', type=int)
    p.add_argument('--entity')
    p.add_argument('--project')
    p.add_argument('--allocation')
    p.add_argument('--replace', action='store_true')
    p = sub.add_parser('meta', help='标注大额性质/资金流性质')
    p.add_argument('id', type=int)
    p.add_argument('--nature', choices=['ordinary', 'planned_large', 'unplanned_large',
                                        'essential_large', 'unknown_large'])
    p.add_argument('--flow', choices=['expense', 'income', 'transfer', 'refund', 'reimbursement'])
    p.add_argument('--detail')
    p.add_argument('--original', type=int)
    p = sub.add_parser('edit', help='修正历史账（金额/分类/项目/日期/类型/备注）')
    p.add_argument('id', type=int)
    p.add_argument('--amount')
    p.add_argument('--category')
    p.add_argument('--item')
    p.add_argument('--date', help='YYYY-MM-DD 或 YYYY-MM-DD HH:MM')
    p.add_argument('--type', dest='type_', choices=['支出', '收入'])
    p.add_argument('--remark')
    p.add_argument('--nature', choices=['ordinary', 'planned_large', 'unplanned_large',
                                        'essential_large', 'unknown_large'])
    p.add_argument('--flow', choices=['expense', 'income', 'transfer', 'refund', 'reimbursement'])
    p = sub.add_parser('delete', help='删除一笔账（默认只预览）')
    p.add_argument('id', type=int)
    p.add_argument('--yes', action='store_true', help='确认删除')
    p = sub.add_parser('budget-set')
    p.add_argument('period')
    p.add_argument('scope', choices=['total', 'category', 'project'])
    p.add_argument('amount')
    p.add_argument('--ref', default='')
    p.add_argument('--coverage', choices=['all', 'ordinary'], default='all')
    p = sub.add_parser('budget-copy', help='从上月继承预算')
    p.add_argument('period')
    p.add_argument('--from', dest='source', required=True)
    p = sub.add_parser('budget-show')
    p.add_argument('period')
    p = sub.add_parser('summary')
    p.add_argument('--start', required=True)
    p.add_argument('--end', required=True)
    p.add_argument('--group', choices=['category', 'entity', 'project', 'nature'], default='category')
    p.add_argument('--include-unattributed', action='store_true', help='实体维度附带“未归属”一行')
    p = sub.add_parser('review')
    p.add_argument('period')
    p = sub.add_parser('archive', help='归档实体/项目（不再参与自动匹配，历史账保留）')
    p.add_argument('name')
    p.add_argument('--kind', choices=['entity', 'project'], default='entity')
    p = sub.add_parser('restore', help='恢复已归档的实体/项目')
    p.add_argument('name')
    p.add_argument('--kind', choices=['entity', 'project'], default='entity')
    p = sub.add_parser('objects', help='列出实体/项目及使用情况')
    p.add_argument('--kind', choices=['entity', 'project'])
    p = sub.add_parser('transactions', help='查流水（最新在前）')
    p.add_argument('--start', required=True)
    p.add_argument('--end', required=True)
    p.add_argument('--entity')
    p.add_argument('--project')
    p.add_argument('--category')
    p.add_argument('--unassigned', action='store_true', help='只看没有关联实体的支出')
    p.add_argument('--limit', type=int, default=50)
    p = sub.add_parser('export', help='导出 CSV（Excel 可直接打开）')
    p.add_argument('out')
    p.add_argument('--start', default='0000-01-01')
    p.add_argument('--end', default='9999-12-31')
    p = sub.add_parser('insight', help='消费诊断：环比/涨跌/小额高频/疑似重复')
    p.add_argument('period')
    p.add_argument('--small', help='小额阈值（元），默认 50')
    p.add_argument('--dup-days', type=int, default=0, help='同事项同金额多少天内算疑似重复；默认 0 = 同一天')
    p = sub.add_parser('recat', help='按当前词表回填分类（默认只改“其他”，先预览）')
    p.add_argument('--apply', action='store_true', help='确认执行')
    p.add_argument('--all', action='store_true', help='连非“其他”的账也重算（慎用）')
    args = ap.parse_args()

    try:
        init(backup=(args.cmd == 'init' and getattr(args, 'backup', False)))
        if args.cmd == 'init':
            out = {'status': 'ok', 'db': str(DB_PATH)}
        elif args.cmd == 'create':
            out = create(args.kind, args.name, args.aliases)
        elif args.cmd == 'link':
            link(args.id, args.entity, args.project, args.allocation, args.replace)
            out = {'linked': args.id}
        elif args.cmd == 'meta':
            set_meta(args.id, args.nature, args.flow, args.detail, args.original)
            out = {'updated': args.id}
        elif args.cmd == 'edit':
            out = edit(args.id, args.amount, args.category, args.item, args.date,
                       args.type_, args.remark, args.nature, args.flow)
        elif args.cmd == 'delete':
            out = delete(args.id, args.yes)
        elif args.cmd == 'budget-set':
            set_budget(args.period, args.scope, args.amount, args.ref, args.coverage)
            out = {'budget_set': args.period}
        elif args.cmd == 'budget-copy':
            out = copy_budget(args.period, args.source)
        elif args.cmd == 'budget-show':
            out = budget_status(args.period)
        elif args.cmd == 'summary':
            out = summary(args.start, args.end, args.group, args.include_unattributed)
        elif args.cmd == 'review':
            out = review(args.period)
        elif args.cmd == 'archive':
            out = archive(args.name, args.kind, restore=False)
        elif args.cmd == 'restore':
            out = archive(args.name, args.kind, restore=True)
        elif args.cmd == 'objects':
            out = objects(args.kind)
        elif args.cmd == 'transactions':
            out = transactions(args.start, args.end, args.entity, args.project,
                               args.category, args.unassigned, args.limit)
        elif args.cmd == 'export':
            out = export_csv(args.out, args.start, args.end)
        elif args.cmd == 'insight':
            out = insight(args.period, args.small, args.dup_days)
        elif args.cmd == 'recat':
            out = recategorize(args.apply, only_other=not args.all)
        else:
            raise ValueError('未知命令')
    except (ValueError, sqlite3.Error) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    cli()
