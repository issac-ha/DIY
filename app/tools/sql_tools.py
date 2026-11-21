"""只读数据查询工具：让模型自己写 SQL 取数

为什么不让 Python 先排好序：选购组合、趋势判断这类问题没有唯一公式，
应该由模型根据问题决定查什么、怎么比较；Python 只保证查到的数据是真的。
"""
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool

# 数据库文件与 app/db/database.py 使用同一个路径
DB_PATH = Path(__file__).resolve().parents[2] / "diy_agent.db"

# 单次查询最多返回多少行、多少字符。行数太多会挤占模型的上下文，
# 聚合类问题应该让模型自己在 SQL 里 GROUP BY，而不是把明细全捞出来。
MAX_ROWS = 100
MAX_CHARS = 8000


@tool
def query_database(sql: str) -> str:
    """
    用只读 SQL 查询本地硬件价格数据库。涉及价格、比价、预算、趋势、组合的问题，
    先调用这个工具取数，再根据返回结果分析。只允许 SELECT 或 WITH 开头的单条语句。

    表结构（SQLite）：
    products(id, name, category, brand, specs, current_price, change, change_pct,
             platform, condition, lowest_shop, lowest_model)
      - category: gpu 显卡 / cpu 处理器 / memory 内存 / ssd 硬盘
      - condition: new 全新 / used 二手
      - platform: jd / taobao / pdd / douyin / xianyu
      - current_price 是该平台最新价；change / change_pct 是相对前一天的涨跌
      - specs 里显卡是 "TSE 跑分 10096"，内存/硬盘是 "每GB 71.16 元"，CPU 为空
    price_history(id, product_id, price, change, change_pct, date, platform)
      - date 是 'YYYY-MM-DD' 文本日期；同一产品同平台同一天只有一条
      - 关联 products：price_history.product_id = products.id
    video_digests(id, bvid, video_date, title, market_summary, news)
      - market_summary 是当日行情归纳；news 是 JSON 字符串数组

    示例：
      - 查某型号各平台最新价：
        SELECT platform, current_price FROM products
        WHERE name = 'RTX 5070' AND condition = 'new' ORDER BY current_price
      - 查预算内的显卡型号：
        SELECT name, MIN(current_price) AS low
        FROM products WHERE category = 'gpu' AND condition = 'new'
        GROUP BY name HAVING low <= 5000 ORDER BY low
      - 取最低价对应的平台（不要 GROUP BY 后直接选 platform，SQLite 会随便取一行）：
        SELECT p.name, p.platform, p.current_price
        FROM products p
        JOIN (SELECT name, MIN(current_price) AS low FROM products
              WHERE category = 'gpu' AND condition = 'new' GROUP BY name) m
          ON p.name = m.name AND p.current_price = m.low
        ORDER BY p.current_price
      - 查最近 30 天某型号每天的最低价：
        SELECT date, MIN(price) AS low FROM price_history ph
        JOIN products p ON p.id = ph.product_id
        WHERE p.name = 'RTX 5070' AND ph.date >= '2026-05-01'
        GROUP BY date ORDER BY date

    :param sql: 一条 SELECT / WITH 查询，最多返回 100 行
    :return: 查询结果的文本表格；出错时返回错误信息，可修正后重试
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        return "SQL 为空，请提供一条 SELECT 查询。"

    # 只放行单条只读语句；连接本身也以只读模式打开，双重保险
    if not re.match(r"^\s*(select|with)\b", statement, re.I):
        return "只允许 SELECT 或 WITH 开头的查询，不能修改数据库。"
    if ";" in statement:
        return "一次只能执行一条 SQL，请去掉中间的分号。"

    if not DB_PATH.exists():
        return f"数据库文件不存在：{DB_PATH}"

    # mode=ro：即使语句绕过校验也写不进去
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = ON")
            latest_date = conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
            # 外层再套一层 LIMIT，防止模型漏写 LIMIT 把整张表捞出来
            cursor = conn.execute(f"SELECT * FROM (\n{statement}\n) LIMIT {MAX_ROWS}")
            columns = [item[0] for item in cursor.description or []]
            rows = cursor.fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"SQL 执行失败：{exc}。请检查表名、字段名和语法后重试。"

    if not rows:
        return f"数据最新日期：{latest_date or '无'}\n查询成功，但没有匹配的数据。"

    lines = [
        f"数据最新日期：{latest_date or '无'}",
        f"返回 {len(rows)} 行（最多 {MAX_ROWS} 行）",
        " | ".join(columns),
    ]
    for row in rows:
        cells = []
        for value in row:
            if value is None:
                cells.append("NULL")
            elif isinstance(value, float):
                cells.append(f"{value:.2f}")
            else:
                cells.append(str(value))
        lines.append(" | ".join(cells))

    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        # 截断时保留表头，提示模型改用聚合查询
        text = text[:MAX_CHARS] + "\n...(结果过长已截断，请用 GROUP BY 聚合或减少列)"
    return text
