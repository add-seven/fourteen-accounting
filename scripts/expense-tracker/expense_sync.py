#!/usr/bin/env python3
"""
记账核心写库脚本
只负责写入本地 SQLite，不负责报表。

CLI:
  python3 expense_sync.py <amount> <type> <category> <item> [remark]

环境变量:
  EXPENSE_DB_PATH 可覆盖默认数据库路径；默认使用 skill 根目录下 expenses.db。
"""
import os
import sqlite3
import sys
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("EXPENSE_DB_PATH") or os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "expenses.db"))

CATEGORIES = {
    "餐饮": ["早餐", "早饭", "午饭", "午餐", "晚饭", "晚餐", "夜宵", "宵夜", "吃饭", "买菜", "餐厅", "外卖", "零食", "水果", "奶茶", "咖啡", "下馆子", "聚餐", "请客", "火锅", "烧烤", "食堂", "自助餐", "KFC", "麦当劳", "汉堡王", "星巴克", "蜜雪冰城", "买水", "烤肠", "馒头", "包子", "点餐"],
    "交通": ["地铁", "公交", "打车", "滴滴", "加油", "停车", "高铁", "机票", "火车票", "火车", "出行", "高速费", "过路费", "共享电动车", "租车", "电动车", "充电", "洗车", "保养", "保险", "购置税", "装潢", "轮胎", "玻璃水", "脚垫", "车牌邮寄", "ETC", "后备箱", "pa壶", "泡沫壶", "虫胶", "擦车"],
    "购物": ["超市", "购物", "衣服", "秋衣", "秋裤", "裤子", "鞋子", "包", "化妆品", "淘宝", "京东", "拼多多", "网购", "商场", "快递", "抽纸", "毛巾", "水桶", "充电线", "垃圾桶", "洗手液", "肥皂架", "底垫", "烧水壶", "电蚊香", "蚊香", "床单", "被罩", "枕套", "枕巾", "鼻毛剪", "削皮刀", "调料盒", "吸油纸", "体脂秤", "洁厕灵", "洗衣液", "洗洁精", "洗发水", "沐浴露", "牙膏", "牙刷", "纸巾", "垃圾袋", "保鲜膜", "保鲜袋", "抹布", "拖把", "扫把", "温度计", "体温计", "爆炸盐", "茶叶", "吸尘器", "手套", "电池"],
    "数码": ["电脑", "笔记本电脑", "笔记本", "键盘", "鼠标", "显示器", "手机", "iphone", "ipad", "平板电脑", "平板", "耳机", "相机", "镜头", "显卡", "内存条", "硬盘", "u盘", "充电宝", "数据线", "充电器", "打印机", "路由器", "智能手表", "手表", "音响", "音箱", "switch", "游戏机", "相机包", "电脑配件"],
    "娱乐": ["电影", "游戏", "KTV", "旅游", "门票", "会员", "订阅", "视频", "Netflix", "爱奇艺", "腾讯视频", "健身房", "健身", "长城", "麻将", "握力器", "网易云", "酷狗音乐"],
    "居住": ["房租", "水电", "物业", "维修", "燃气", "暖气", "取暖", "酒店", "住宿", "宾馆", "民宿", "电费", "水费", "燃气费", "水卡"],
    "学习": ["书籍", "书", "课程", "培训", "学费", "文具", "电子书", "kindle", "gpt", "claude", "deepseek", "chatgpt"],
    "医疗": ["医院", "药店", "药品", "门诊", "体检", "医生", "药"],
    "通讯": ["手机话费", "充话费", "话费", "流量", "宽带", "座机", "宽带费", "流量套餐"],
    "烟酒": ["买烟", "烟", "酒", "啤酒", "白酒", "香烟"],
    "礼品": ["礼品", "礼物", "送礼", "中秋", "月饼", "礼盒", "伴手礼", "稻香村", "英红九号", "金典有机", "买花", "鲜花", "花束"]
}

VALID_TYPES = {"支出", "收入"}


def _build_keyword_index():
    """关键词 → (分类, 声明顺序, 长度)。

    同一关键词若出现在多个分类里，以先声明者为准，避免同一个词随机归类。
    """
    index = {}
    for rank, (category, keywords) in enumerate(CATEGORIES.items()):
        for keyword in keywords:
            kw = keyword.lower().strip()
            if kw and kw not in index:
                index[kw] = (category, rank, len(kw))
    return index


KEYWORD_INDEX = _build_keyword_index()


def auto_categorize(item):
    """按最长关键词匹配分类；长度相同则按词表声明顺序，结果稳定可复现。"""
    item_lower = str(item or "").lower()
    best = None
    for kw, (category, rank, length) in KEYWORD_INDEX.items():
        if kw in item_lower:
            score = (length, -rank)
            if best is None or score > best[0]:
                best = (score, category)
    return best[1] if best else "其他"


def category_names():
    return list(CATEGORIES)


def normalize_time(expense_time=None):
    """标准化时间为 YYYY-MM-DD HH:MM:SS；只给日期则补 12:00:00。"""
    if not expense_time:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expense_time = str(expense_time).strip().replace("T", " ")
    if " " not in expense_time:
        return expense_time + " 12:00:00"
    if len(expense_time) == 16:  # YYYY-MM-DD HH:MM
        return expense_time + ":00"
    return expense_time[:19]


def init_db():
    """初始化数据库表。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_time TEXT NOT NULL,
        amount REAL NOT NULL,
        type TEXT NOT NULL,
        category TEXT NOT NULL,
        item TEXT NOT NULL,
        remark TEXT DEFAULT '',
        bitable_record_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        sync_status TEXT DEFAULT 'synced'
    )
    ''')
    conn.commit()
    conn.close()


def add_expense(amount, expense_type, category, item, remark="", expense_time=None):
    """添加记账记录，返回记录 ID。"""
    init_db()

    amount = float(amount)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    if expense_type not in VALID_TYPES:
        raise ValueError("类型必须是 '支出' 或 '收入'")
    if not item:
        raise ValueError("项目名称不能为空")

    if not category or category == "其他":
        category = auto_categorize(item)
    expense_time = normalize_time(expense_time)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
    INSERT INTO expenses (expense_time, amount, type, category, item, remark)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (expense_time, amount, expense_type, category or "其他", item, remark or ""))
    record_id = c.lastrowid
    conn.commit()
    conn.close()

    print(f"[本地] 写入成功，id={record_id}，时间={expense_time}")
    return record_id


def main(argv):
    if len(argv) < 5:
        print("用法: python3 expense_sync.py <金额> <支出|收入> <分类> <项目> [备注]", file=sys.stderr)
        return 1
    try:
        amount = float(argv[1])
        expense_type = argv[2]
        category = argv[3]
        item = argv[4]
        remark = argv[5] if len(argv) > 5 else ""
        add_expense(amount, expense_type, category, item, remark)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
