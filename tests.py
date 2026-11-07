import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

S = Path(__file__).parent / 'scripts' / 'expense-tracker'
sys.path.insert(0, str(S))

import expense_nl  # noqa: E402
import expense_sync  # noqa: E402
import expense_ledger  # noqa: E402
import expense_report as report  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'ledger.db'
        os.environ['EXPENSE_DB_PATH'] = str(self.db)
        expense_sync.DB_PATH = str(self.db)
        expense_ledger.DB_PATH = self.db
        expense_ledger.init()
        self.ledger = expense_ledger
        self.nl = expense_nl

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, amount, item='午饭', category='餐饮', when='2026-11-05'):
        return expense_sync.add_expense(amount, '支出', category, item, expense_time=when)

    def run_nl(self, text, *extra):
        return self.nl.main(['expense_nl.py', text, *extra])


class ParseTests(Base):
    """金额解析：型号数字不能被当金额。"""

    def test_model_number_not_amount(self):
        p = self.nl.parse_expense('买iPhone15 6999')
        self.assertEqual(p.amount, 6999)
        self.assertFalse(p.needs_confirm)

    def test_multiple_numbers_needs_confirm(self):
        p = self.nl.parse_expense('小米14 4299')
        self.assertEqual(p.amount, 4299)
        self.assertTrue(p.needs_confirm, '多个无单位金额必须要求确认，不能静默写库')

    def test_unit_amount_wins(self):
        p = self.nl.parse_expense('2杯咖啡 一共 36元')
        self.assertEqual(p.amount, 36)
        self.assertFalse(p.needs_confirm)

    def test_missing_amount(self):
        p = self.nl.parse_expense('午饭')
        self.assertIsNone(p.amount)
        self.assertTrue(p.needs_confirm)

    def test_wan_unit(self):
        p = self.nl.parse_expense('买家具1.2万')
        self.assertEqual(p.amount, 12000)

    def test_kou_wei_yuan(self):
        p = self.nl.parse_expense('午饭25块5')
        self.assertEqual(p.amount, 25.5)
        self.assertEqual(p.item, '午饭')

    def test_thousand_separator_not_split(self):
        self.assertEqual(self.run_nl('电脑1,200元'), 0)
        with self.ledger.connect() as con:
            rows = con.execute('SELECT amount FROM expenses').fetchall()
        self.assertEqual([r[0] for r in rows], [1200.0])

    def test_pure_income_without_item(self):
        self.assertEqual(self.run_nl('收入8000'), 0)
        with self.ledger.connect() as con:
            row = con.execute('SELECT amount,type,item FROM expenses').fetchone()
        self.assertEqual((row[0], row[1], row[2]), (8000.0, '收入', '收入'))

    def test_multi_items_reject_explicit_entity(self):
        self.ledger.create('person', '张三')
        self.assertEqual(self.run_nl('加油300，门票100', '--entity', '张三'), 2)
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 0)

    def test_weekday_date(self):
        p = self.nl.parse_expense('周五午饭25', now=datetime(2026, 11, 7, 10, 0))
        self.assertEqual(p.expense_time, '2026-11-06 12:00:00')
        self.assertEqual(p.item, '午饭')

    def test_last_week_weekday(self):
        p = self.nl.parse_expense('上周三打车30', now=datetime(2026, 11, 7, 10, 0))
        self.assertEqual(p.expense_time, '2026-10-28 12:00:00')

    def test_today_keeps_real_time(self):
        p = self.nl.parse_expense('今天午饭25')
        self.assertIsNone(p.expense_time)


class SplitTests(Base):
    def test_split_multi_items(self):
        self.assertEqual(self.nl.split_items('午饭20，晚饭19'), ['午饭20', '晚饭19'])

    def test_split_with_plus(self):
        self.assertEqual(self.nl.split_items('夜宵41➕17'), ['夜宵41', '17'])

    def test_no_split_without_number(self):
        self.assertEqual(self.nl.split_items('午饭，加个鸡腿'), ['午饭，加个鸡腿'])

    def test_multi_items_write_two_rows(self):
        self.assertEqual(self.run_nl('午饭20，晚饭19'), 0)
        with self.ledger.connect() as con:
            rows = con.execute('SELECT amount FROM expenses ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in rows], [20.0, 19.0])

    def test_plus_item_inherits_previous_label(self):
        self.assertEqual(self.run_nl('夜宵41➕17'), 0)
        with self.ledger.connect() as con:
            rows = con.execute('SELECT amount,item FROM expenses ORDER BY id').fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(41.0, '夜宵'), (17.0, '夜宵')])

    def test_multi_items_blocked_when_ambiguous(self):
        self.assertEqual(self.run_nl('小米14 4299，午饭20'), 2)
        with self.ledger.connect() as con:
            n = con.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
        self.assertEqual(n, 0, '有疑问时不能只写一半')

    def test_date_carried_to_all_segments(self):
        self.run_nl('昨天午饭20，晚饭19')
        with self.ledger.connect() as con:
            rows = con.execute('SELECT expense_time FROM expenses ORDER BY id').fetchall()
        self.assertTrue(all(r[0].startswith('2026-11-06') for r in rows))


class EntityTests(Base):
    def test_entity_project_and_no_double_count(self):
        self.ledger.create('asset', '我的汽车', '车子')
        self.ledger.create('person', '张三')
        self.ledger.create('project', '家庭自驾游', '自驾')
        tx = self.add('300', '自驾车子加油', '交通')
        linked = self.ledger.auto_link(tx, '自驾车子加油')
        self.assertEqual((linked['entity'], linked['project']), ('我的汽车', '家庭自驾游'))
        self.assertFalse(linked['multi'])
        self.ledger.link(tx, entity='张三')
        self.assertEqual(self.ledger.summary('2026-11-01', '2026-12-01', 'category')[0]['amount'], '300.00')
        self.assertEqual(self.ledger.summary('2026-11-01', '2026-12-01', 'project')[0]['amount'], '300.00')
        self.assertEqual(len(self.ledger.summary('2026-11-01', '2026-12-01', 'entity')), 2)

    def test_allocation_rolls_back(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('100')
        self.ledger.link(tx, entity='张三', allocation='70')
        with self.assertRaises(ValueError):
            self.ledger.link(tx, entity='李四', allocation='40')
        with self.ledger.connect() as con:
            self.assertEqual(con.execute(
                'SELECT COUNT(*) FROM transaction_entities WHERE transaction_id=?', (tx,)).fetchone()[0], 1)

    def test_allocation_reflected_in_report(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三', allocation='70')
        self.ledger.link(tx, entity='李四', allocation='30')
        data = {r['name']: r['amount'] for r in self.ledger.summary('2026-11-01', '2026-12-01', 'entity')}
        self.assertEqual(data, {'张三': '70.00', '李四': '30.00'})

    def test_unallocated_entity_gets_zero_when_split(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三', allocation='70')
        self.ledger.link(tx, entity='李四')
        data = {r['name']: r['amount'] for r in self.ledger.summary('2026-11-01', '2026-12-01', 'entity')}
        self.assertEqual(data, {'张三': '70.00'})

    def test_unattributed_row(self):
        self.ledger.create('asset', '我的汽车', '车子')
        tx = self.add('300', '车子加油', '交通')
        self.ledger.auto_link(tx, '车子加油')
        self.add('25', '午饭', '餐饮')
        data = self.ledger.summary('2026-11-01', '2026-12-01', 'entity', True)
        self.assertEqual(data[0]['name'], '我的汽车')
        self.assertEqual(data[-1]['name'], '未归属（可视为自己/其他）')
        self.assertEqual(data[-1]['amount'], '25.00')

    def test_ambiguous_alias_no_auto_link(self):
        self.ledger.create('person', '张三', '老张')
        self.ledger.create('person', '张四', '老张')
        self.assertEqual(self.ledger.match_all('请老张吃饭', 'entities'), [])

    def test_unattributed_row_hidden_when_zero(self):
        self.ledger.create('person', '张三')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三', allocation='100')
        data = self.ledger.summary('2026-11-01', '2026-12-01', 'entity', True)
        self.assertEqual([r['name'] for r in data], ['张三'])

    def test_duplicate_create_merges_aliases(self):
        self.ledger.create('asset', '我的汽车', '保养')
        out = self.ledger.create('asset', '我的汽车', '车子')
        self.assertIn('已存在', out['status'])
        self.assertEqual(sorted(out['aliases']), sorted(['保养', '车子']))

    def test_single_char_alias_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.create('person', '自己', '我')

    def test_legacy_single_char_alias_not_matched(self):
        self.ledger.create('person', '自己')
        with self.ledger.connect() as con:
            con.execute("UPDATE entities SET aliases=? WHERE name='自己'", ('["我"]',))
        self.assertEqual(self.ledger.match_all('自我提升课程', 'entities'), [])
        self.assertEqual(self.ledger.match_all('自己午饭', 'entities'), ['自己'])


class BudgetTests(Base):
    def test_budgets_and_transfer_exclusion(self):
        self.add('30')
        b = self.add('100')
        self.ledger.set_meta(b, flow='transfer')
        self.ledger.set_budget('2026-11', 'total', '100')
        self.assertEqual(self.ledger.budget_status('2026-11')[0]['spent'], '30.00')

    def test_refund_nets_budget(self):
        self.add('200')
        self.run_nl('退款50')
        self.ledger.set_budget('2026-11', 'total', '500')
        self.assertEqual(self.ledger.budget_status('2026-11')[0]['spent'], '150.00')

    def test_refund_auto_flow_flag(self):
        self.run_nl('退款50')
        with self.ledger.connect() as con:
            flow = con.execute('SELECT flow_kind FROM transaction_meta').fetchone()[0]
        self.assertEqual(flow, 'refund')

    def test_budget_copy(self):
        self.ledger.set_budget('2026-10', 'total', '4000')
        self.ledger.set_budget('2026-10', 'category', '600', '餐饮')
        out = self.ledger.copy_budget('2026-11', '2026-10')
        self.assertEqual(out['copied'], 2)
        self.assertEqual(len(self.ledger.budget_status('2026-11')), 2)

    def test_unknown_category_budget_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.set_budget('2026-11', 'category', '600', '餐饭')

    def test_invalid_period_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.budget_status('2026-1')
        with self.assertRaises(ValueError):
            self.ledger.month_range('2026-13')

    def test_category_ranking_excludes_income(self):
        self.add('25', '午饭', '餐饮')
        self.run_nl('工资8000')
        names = [r['name'] for r in self.ledger.summary('2026-11-01', '2026-12-01', 'category')]
        self.assertNotIn('收入', names)


class EditTests(Base):
    def test_edit_amount_and_category(self):
        tx = self.add('15', 'iPhone', '其他')
        out = self.ledger.edit(tx, amount='6999', category='购物', item='iPhone15')
        self.assertEqual(out['changed']['amount'], [15.0, 6999.0])
        data = self.ledger.summary('2026-11-01', '2026-12-01', 'category')
        self.assertEqual(data[0]['name'], '购物')
        self.assertEqual(data[0]['amount'], '6999.00')

    def test_edit_unknown_id(self):
        with self.assertRaises(ValueError):
            self.ledger.edit(999, amount='1')

    def test_edit_amount_below_allocation_rejected(self):
        self.ledger.create('person', '张三')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三', allocation='80')
        with self.assertRaises(ValueError):
            self.ledger.edit(tx, amount='50')

    def test_delete_preview_then_confirm(self):
        self.ledger.create('person', '张三')
        tx = self.add('30')
        self.ledger.link(tx, entity='张三')
        out = self.ledger.delete(tx)
        self.assertIn('would_delete', out)
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 1)
        out = self.ledger.delete(tx, True)
        self.assertEqual(out['deleted']['id'], tx)
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 0)
            self.assertEqual(con.execute(
                'SELECT COUNT(*) FROM transaction_entities').fetchone()[0], 0)


class BackupTests(Base):
    def test_init_backup_creates_copy(self):
        self.add('20')
        self.ledger.init(backup=True)
        baks = list(self.db.parent.glob('ledger.db.bak-*'))
        self.assertEqual(len(baks), 1)
        self.assertGreater(baks[0].stat().st_size, 0)


class LegacyTests(Base):
    def test_legacy_preserved_and_idempotent(self):
        tx = self.add('18')
        self.ledger.init()
        self.ledger.init()
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT amount FROM expenses WHERE id=?', (tx,)).fetchone()[0], 18.0)

    def test_nature_summary(self):
        tx = self.add('4500', '汽车保险', '交通')
        self.ledger.set_meta(tx, nature='planned_large')
        self.add('30')
        data = {r['name']: r['amount'] for r in self.ledger.summary('2026-11-01', '2026-12-01', 'nature')}
        self.assertEqual(data, {'planned_large': '4500.00', 'ordinary': '30.00'})


class CategoryTests(Base):
    def test_digital_category(self):
        for item in ['买键盘399', '显示器1299', 'iPhone15', '路由器199', '耳机199', '笔记本电脑6999']:
            self.assertEqual(expense_sync.auto_categorize(item), '数码', item)

    def test_phone_bill_stays_comm(self):
        self.assertEqual(expense_sync.auto_categorize('手机话费100'), '通讯')
        self.assertEqual(expense_sync.auto_categorize('宽带费80'), '通讯')

    def test_no_duplicate_keyword_across_categories(self):
        seen = {}
        for cat, kws in expense_sync.CATEGORIES.items():
            for kw in kws:
                seen.setdefault(kw.lower(), set()).add(cat)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(dups, {}, '同一关键词出现在多个分类会导致归类不稳定')

    def test_recat_preview_then_apply(self):
        keep = self.add('30', '午饭', '餐饮')
        with self.ledger.connect() as con:
            cur = con.execute("INSERT INTO expenses(expense_time,amount,type,category,item) "
                              "VALUES('2026-11-05 12:00:00',399,'支出','其他','键盘')")
            tx = cur.lastrowid
        out = self.ledger.recategorize()
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['changes'][0]['to'], '数码')
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT category FROM expenses WHERE id=?',
                                         (tx,)).fetchone()[0], '其他')
        self.ledger.recategorize(apply=True)
        with self.ledger.connect() as con:
            self.assertEqual(con.execute('SELECT category FROM expenses WHERE id=?',
                                         (tx,)).fetchone()[0], '数码')
            self.assertEqual(con.execute('SELECT category FROM expenses WHERE id=?',
                                         (keep,)).fetchone()[0], '餐饮')


class MultiEntityTests(Base):
    def test_two_entities_matched(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('200', '给张三李四买礼物', '礼品')
        out = self.ledger.auto_link(tx, '给张三李四买礼物')
        self.assertEqual(out['entities'], ['张三', '李四'])
        self.assertTrue(out['multi'])
        names = {r['name'] for r in self.ledger.summary('2026-11-01', '2026-12-01', 'entity')}
        self.assertEqual(names, {'张三', '李四'})

    def test_multi_entity_not_double_counted(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('200', '张三李四聚餐', '餐饮')
        self.ledger.auto_link(tx, '张三李四聚餐')
        a = self.ledger.attribution('2026-11-01', '2026-12-01')
        self.assertEqual(self.ledger.fmt(a['attributed']), '200.00')
        self.assertEqual(self.ledger.fmt(a['unattributed']), '0.00')

    def test_longer_name_shadows_short(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '张三丰')
        self.assertEqual(self.ledger.match_all('张三丰请客', 'entities'), ['张三丰'])

    def test_ambiguous_same_length_no_match(self):
        self.ledger.create('person', '张三', '老张')
        self.ledger.create('person', '张四', '老张')
        self.assertEqual(self.ledger.match_all('老张吃饭', 'entities'), [])


class ArchiveTests(Base):
    def test_archived_entity_not_matched(self):
        self.ledger.create('asset', '我的汽车', '车子')
        self.assertEqual(self.ledger.match_all('车子加油', 'entities'), ['我的汽车'])
        self.ledger.archive('我的汽车')
        self.assertEqual(self.ledger.match_all('车子加油', 'entities'), [])
        self.ledger.archive('我的汽车', restore=True)
        self.assertEqual(self.ledger.match_all('车子加油', 'entities'), ['我的汽车'])

    def test_archived_project_not_matched(self):
        self.ledger.create('project', '家庭自驾游', '自驾')
        self.ledger.archive('家庭自驾游', kind='project')
        self.assertEqual(self.ledger.match_all('露营住宿', 'projects'), [])
        self.assertEqual(self.ledger.objects('project')[0]['status'], 'archived')

    def test_archive_keeps_history(self):
        self.ledger.create('person', '张三')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三')
        out = self.ledger.archive('张三')
        self.assertEqual(out['linked_transactions'], 1)
        self.assertEqual(self.ledger.summary('2026-11-01', '2026-12-01', 'entity')[0]['amount'], '100.00')

    def test_archive_unknown_raises(self):
        with self.assertRaises(ValueError):
            self.ledger.archive('不存在的人')


class ExportTests(Base):
    def test_csv_export(self):
        self.ledger.create('person', '张三')
        tx = self.add('100', '聚餐', '餐饮')
        self.ledger.link(tx, entity='张三', allocation='70')
        path = self.db.parent / 'out.csv'
        out = self.ledger.export_csv(str(path))
        self.assertEqual(out['rows'], 1)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'), 'Excel 打开中文需要 UTF-8 BOM')
        text = raw.decode('utf-8-sig')
        self.assertIn('ID,日期,金额', text)
        self.assertIn('张三:70.00', text)


class TransactionQueryTests(Base):
    def test_unassigned_filter(self):
        self.ledger.create('person', '张三')
        a = self.add('100', '聚餐', '餐饮')
        self.add('25', '午饭', '餐饮')
        self.ledger.link(a, entity='张三')
        rows = self.ledger.transactions('2026-11-01', '2026-12-01', unassigned=True)
        self.assertEqual([r['item'] for r in rows], ['午饭'])

    def test_filter_by_entity(self):
        self.ledger.create('person', '张三')
        a = self.add('100', '聚餐', '餐饮')
        self.add('25', '午饭', '餐饮')
        self.ledger.link(a, entity='张三')
        rows = self.ledger.transactions('2026-11-01', '2026-12-01', entity='张三')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['entities'], '张三')


class InsightTests(Base):
    def test_mom_and_category_delta(self):
        self.add('100', '午饭', '餐饮', '2026-10-10')
        self.add('200', '午饭', '餐饮')
        ins = self.ledger.insight('2026-11')
        self.assertEqual(ins['previous_total'], '100.00')
        self.assertEqual(ins['total'], '200.00')
        self.assertEqual(ins['mom_percent'], 100.0)
        self.assertEqual(ins['category_growth'][0]['category'], '餐饮')
        self.assertEqual(ins['category_growth'][0]['delta'], '100.00')

    def test_small_and_duplicate(self):
        self.add('10', '咖啡', '餐饮')
        self.add('10', '咖啡', '餐饮')
        self.add('300', '键盘', '数码')
        ins = self.ledger.insight('2026-11', small='20')
        self.assertEqual(ins['small']['count'], 2)
        self.assertEqual(ins['small']['sum'], '20.00')
        self.assertEqual(ins['duplicates'][0]['item'], '咖啡')
        self.assertEqual(ins['duplicates'][0]['count'], 2)
        self.assertEqual(ins['largest']['item'], '键盘')

    def test_no_duplicate_when_far_apart(self):
        self.add('10', '咖啡', '餐饮', '2026-11-01')
        self.add('10', '咖啡', '餐饮', '2026-11-20')
        self.assertEqual(self.ledger.insight('2026-11')['duplicates'], [])

    def test_wider_window_finds_cross_day_duplicate(self):
        self.add('10', '咖啡', '餐饮', '2026-11-01')
        self.add('10', '咖啡', '餐饮', '2026-11-20')
        ins = self.ledger.insight('2026-11', dup_days=30)
        self.assertEqual(ins['duplicates'][0]['item'], '咖啡')

    def test_week_window_compares_previous_week(self):
        self.add('600', '聚餐', '餐饮', '2026-10-30')
        self.add('60', '午饭', '餐饮', '2026-11-03')
        ins = self.ledger.insight(start='2026-11-02', end='2026-11-09')
        self.assertEqual(ins['mom_label'], '环比上周')
        self.assertEqual(ins['previous_total'], '600.00')
        self.assertEqual(ins['total'], '60.00')


class TemplateTests(Base):
    """输出模板：回执符号/日期、报表口径一致、不出现道德判断。"""

    def _p(self, **kw):
        base = dict(text='', amount=25, type='支出', category='餐饮', item='午饭',
                    remark='', expense_time='2026-11-05 12:00:00', confidence=1.0,
                    needs_confirm=False, reason='ok')
        base.update(kw)
        return self.nl.ParsedExpense(**base)

    def test_receipt_omits_today_and_shows_other_days(self):
        p = self._p()
        self.assertNotIn('11-05', self.nl._confirm_line(p, 7, today='2026-11-05'))
        self.assertIn('11-05', self.nl._confirm_line(p, 7, today='2026-11-07'))

    def test_receipt_signs(self):
        self.assertTrue(self.nl._confirm_line(self._p(type='收入', category='收入'), 1).startswith('+¥'))
        refund = self.nl._confirm_line(self._p(flow='refund'), 2)
        self.assertTrue(refund.startswith('↩¥'), refund)
        self.assertIn('退款', refund)

    def test_receipt_money_has_thousands_separator(self):
        line = self.nl._confirm_line(self._p(amount=6999, item='iPhone15'), 3)
        self.assertIn('¥6,999', line)

    def test_multi_item_prints_total(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_nl('午饭20，晚饭19')
        self.assertIn('合计 ¥39（2 笔）', buf.getvalue())

    def test_no_moral_judgement_in_day_report(self):
        self.add('800', '衣服', '购物')
        text = report.day_report('2026-11-05')
        for word in ('偏高', '注意控制', '保持良好', '建议'):
            self.assertNotIn(word, text)

    def test_insight_total_is_net(self):
        self.add('100', '衣服', '购物')
        expense_sync.add_expense(30, '收入', '收入', '退款', expense_time='2026-11-06')
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO transaction_meta(transaction_id, flow_kind) "
                    "VALUES((SELECT MAX(id) FROM expenses), 'refund')")
        con.commit()
        con.close()
        self.assertEqual(self.ledger.insight('2026-11')['total'], '70.00')

    def test_week_and_month_agree_on_wording_of_total(self):
        self.add('100', '衣服', '购物')
        week = report.week_report('2026-11-05')
        month = report.month_report('2026-11')
        self.assertIn('本周支出：¥100', week)
        self.assertIn('支出：¥100', month)

    def test_daily_average_is_whole_yuan(self):
        # 100/6 会除不尽，旧实现输出 ¥16.67，与其余整数金额粒度不一致
        self.add('100', '衣服', '购物', '2026-11-02')
        week = report.week_report('2026-11-05')
        avg = re.search(r'日均 ¥([\d,]+(?:\.\d+)?)', week).group(1)
        self.assertNotIn('.', avg, week)

    def test_tiny_share_is_not_shown_as_zero(self):
        self.assertEqual(report.pct(4, 1000), '<1%')
        self.assertEqual(report.pct(0, 1000), '0%')
        self.assertEqual(report.pct(500, 1000), '50%')

    def test_budget_alarm_not_repeated_in_alerts_section(self):
        # 预算超支已在「💰 预算」段表达，⚠️ 关注 段不应再说一次
        self.ledger.set_budget('2026-11', 'total', '10')
        self.add('800', '衣服', '购物')
        month = report.month_report('2026-11')
        self.assertEqual(month.count('超支'), 0, month)
        self.assertIn('超 ¥790', month)

    def test_mom_label_month(self):
        self.add('100', '衣服', '购物', '2026-11-05')
        self.add('50', '衣服', '购物', '2026-10-05')
        self.assertIn('环比上月', report.month_report('2026-11'))

    def test_mom_label_week(self):
        self.add('100', '衣服', '购物', '2026-11-05')
        self.add('50', '衣服', '购物', '2026-10-28')
        self.assertIn('环比上周', report.week_report('2026-11-05'))


class LinkingTests(Base):
    """关联写入：项目不能因为句中没人物就被丢掉；重复 link 不能清空已设的分摊。"""

    def count(self, table, tx):
        con = sqlite3.connect(str(self.db))
        try:
            return con.execute(f'SELECT COUNT(*) FROM {table} WHERE transaction_id=?',
                               (tx,)).fetchone()[0]
        finally:
            con.close()

    def test_project_linked_without_entity(self):
        self.ledger.create('project', '家庭自驾游', '自驾')
        tx = self.add('300', '自驾加油', '交通')
        out = self.ledger.auto_link(tx, '自驾加油')
        self.assertIsNone(out['entity'])
        self.assertEqual(out['project'], '家庭自驾游')
        self.assertEqual(self.count('transaction_projects', tx), 1)
        rows = self.ledger.summary('2026-11-01', '2026-12-01', 'project')
        self.assertEqual(rows[0]['name'], '家庭自驾游')
        self.assertEqual(rows[0]['amount'], '300.00')

    def test_explicit_project_linked(self):
        self.ledger.create('project', '家庭自驾游')
        tx = self.add('400', '酒店', '居住')
        self.ledger.auto_link(tx, '酒店', None, '家庭自驾游')
        self.assertEqual(self.count('transaction_projects', tx), 1)

    def test_entity_and_project_both_linked(self):
        self.ledger.create('asset', '我的汽车', '车子')
        self.ledger.create('project', '家庭自驾游', '自驾')
        tx = self.add('300', '自驾车子加油', '交通')
        self.ledger.auto_link(tx, '自驾车子加油')
        self.assertEqual(self.count('transaction_entities', tx), 1)
        self.assertEqual(self.count('transaction_projects', tx), 1)

    def test_project_budget_sees_linked_spend(self):
        self.ledger.create('project', '家庭自驾游', '自驾')
        self.ledger.set_budget('2026-11', 'project', 6000, '家庭自驾游')
        tx = self.add('300', '自驾加油', '交通')
        self.ledger.auto_link(tx, '自驾加油')
        spent = {b['name']: b['spent'] for b in self.ledger.budget_status('2026-11')}
        self.assertEqual(spent['家庭自驾游'], '300.00')

    def test_relink_keeps_allocation(self):
        self.ledger.create('person', '张三')
        tx = self.add('500', '聚餐', '餐饮')
        self.ledger.link(tx, '张三', allocation=50)
        self.ledger.link(tx, '张三')          # 再链一次（没带分摊）不能把 50 抹成“全额”
        con = sqlite3.connect(str(self.db))
        try:
            alloc = con.execute('SELECT allocated_cents FROM transaction_entities '
                                'WHERE transaction_id=?', (tx,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(alloc, 5000)

    def test_no_link_skips_auto_link(self):
        self.ledger.create('person', '张三')
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_nl('给张三买水5', '--no-link')
        self.assertIn('¥5', buf.getvalue())
        con = sqlite3.connect(str(self.db))
        try:
            n = con.execute('SELECT COUNT(*) FROM transaction_entities').fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0)

    def test_refund_without_original_is_flagged(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_nl('退款200')
        out = buf.getvalue()
        self.assertIn('未挂原单', out)
        self.assertIn('--original', out)

    def test_match_all_keeps_second_occurrence(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '张三丰')
        self.assertEqual(self.ledger.match_all('张三丰请张三吃饭', 'entities'), ['张三丰', '张三'])

    def test_relink_preview_then_apply(self):
        self.ledger.create('project', '家庭自驾游', '自驾')
        tx = self.add('300', '自驾加油', '交通')
        preview = self.ledger.relink('2026-11-01', '2026-12-01')
        self.assertEqual(preview['pending'], 1)
        self.assertEqual(self.count('transaction_projects', tx), 0)   # 预览不写库
        self.ledger.relink('2026-11-01', '2026-12-01', apply=True)
        self.assertEqual(self.count('transaction_projects', tx), 1)
        self.assertEqual(self.ledger.relink('2026-11-01', '2026-12-01')['pending'], 0)


class CrossTests(Base):
    def test_cross_category_entity(self):
        self.ledger.create('person', '张三')
        self.add('25', '午饭', '餐饮')
        tx = self.add('120', '请张三吃饭', '餐饮')
        self.ledger.auto_link(tx, '请张三吃饭')
        out = self.ledger.cross('2026-11-01', '2026-12-01', 'category', 'entity')
        row = {r['row']: r for r in out['matrix']}['餐饮']
        self.assertEqual(row['cells']['张三'], '120.00')
        self.assertEqual(row['cells']['未归属（可视为自己/其他）'], '25.00')
        self.assertEqual(row['total'], '145.00')

    def test_cross_rejects_same_dimension(self):
        with self.assertRaises(ValueError):
            self.ledger.cross('2026-11-01', '2026-12-01', 'entity', 'entity')

    def test_cross_row_total_dedups_multi_entity(self):
        self.ledger.create('person', '张三')
        self.ledger.create('person', '李四')
        tx = self.add('200', '张三李四聚餐', '餐饮')
        self.ledger.auto_link(tx, '张三李四聚餐')
        out = self.ledger.cross('2026-11-01', '2026-12-01', 'category', 'entity')
        row = out['matrix'][0]
        self.assertEqual(row['total'], '200.00')       # 行合计按交易去重
        self.assertEqual(out['col_totals']['张三'], '200.00')


if __name__ == '__main__':
    unittest.main()
