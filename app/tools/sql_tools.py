"""只读数据查询工具：让模型自己写 SQL 取数

为什么不让 Python 先排好序：选购组合、趋势判断这类问题没有唯一公式，
应该由模型根据问题决定查什么、怎么比较；Python 只保证查到的数据是真的。
"""
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool
from sqlalchemy.engine import make_url

from app.config import DATABASE_URL

# 单次查询最多返回多少行、多少字符。行数太多会挤占模型的上下文，
# 聚合类问题应该让模型自己在 SQL 里 GROUP BY，而不是把明细全捞出来。
MAX_ROWS = 100
MAX_CHARS = 8000

# 与 app/db/database.py 使用同一个连接串，这里只解析方言和连接参数。
# 模块加载时解析一次，切库需要重启 API 才会生效。
_DB_URL = make_url(DATABASE_URL)
_DB_BACKEND = _DB_URL.get_backend_name()

# 本地 SQLite 文件路径，只有方言为 sqlite 时才会用到
_SQLITE_PATH = Path(__file__).resolve().parents[2] / "diy_agent.db"


def _run_sqlite(statement: str) -> tuple[list[str], list[tuple], object]:
    """SQLite 只读查询：用 mode=ro 打开文件，再加 PRAGMA 兜底"""
    uri = f"file:{_SQLITE_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA query_only = ON")
        latest = conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
        cursor = conn.execute(f"SELECT * FROM (\n{statement}\n) AS _q LIMIT {MAX_ROWS}")
        columns = [item[0] for item in cursor.description or []]
        return columns, cursor.fetchall(), latest
    finally:
        conn.close()


def _run_mysql(statement: str) -> tuple[list[str], list[tuple], object]:
    """
    MySQL 只读查询

    这里不用 SQLAlchemy 的 engine，而是自己开一条独立连接。
    原因是只读设置属于会话级状态，如果借用连接池里的连接，
    只读状态会残留下去，导致采集进程的写操作被服务端拒绝。
    独立连接用完即关，物理连接销毁，不会影响别人。
    """
    import pymysql

    conn = pymysql.connect(
        host=_DB_URL.host or "127.0.0.1",
        port=_DB_URL.port or 3306,
        user=_DB_URL.username,
        password=_DB_URL.password,
        database=_DB_URL.database,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cursor:
            # 用 START TRANSACTION READ ONLY 而不是 SET TRANSACTION READ ONLY：
            # 后者只对下一个事务生效，第一条 SELECT 用完就失效，
            # 只能靠显式开启只读事务覆盖整段查询
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT MAX(date) FROM price_history")
            row = cursor.fetchone()
            latest = row[0] if row else None
            cursor.execute(f"SELECT * FROM (\n{statement}\n) AS _q LIMIT {MAX_ROWS}")
            columns = [item[0] for item in cursor.description or []]
            return columns, cursor.fetchall(), latest
    finally:
        # 只读事务不需要提交，回滚即结束
        conn.rollback()
        conn.close()


def _execute(statement: str) -> tuple[list[str], list[tuple], object]:
    """按当前数据库方言选择只读执行方式"""
    if _DB_BACKEND == "sqlite":
        return _run_sqlite(statement)
    if _DB_BACKEND == "mysql":
        return _run_mysql(statement)
    raise RuntimeError(f"不支持的数据库类型: {_DB_BACKEND}")


@tool
def query_database(sql: str) -> str:
    """
    用只读 SQL 查询硬件价格数据库。涉及价格、比价、预算、趋势、组合的问题，
    先调用这个工具取数，再根据返回结果分析。只允许 SELECT 或 WITH 开头的单条语句。

    表结构：
    products(id, name, category, brand, specs, current_price, `change`, `change_pct`,
             platform, `condition`, lowest_shop, lowest_model)
      - category: gpu 显卡 / cpu 处理器 / memory 内存 / ssd 硬盘
      - `condition`: new 全新 / used 二手（在 MySQL 中是保留字，必须加反引号）
      - `change` / `change_pct`：相对前一天的涨跌（在 MySQL 中是保留字，必须加反引号）
      - platform: jd / taobao / pdd / douyin / xianyu
      - current_price 是该平台最新价
      - specs 里显卡是 "TSE 跑分 10096"，内存/硬盘是 "每GB 71.16 元"，CPU 为空
    price_history(id, product_id, price, `change`, `change_pct`, date, platform)
      - date 是日期类型；同一产品同平台同一天只有一条
      - 关联 products：price_history.product_id = products.id
    video_digests(id, bvid, video_date, title, market_summary, news)
      - market_summary 是当日行情归纳；news 是 JSON 字符串数组

    示例：
      - 查某型号各平台最新价：
        SELECT platform, current_price FROM products
        WHERE name = 'RTX 5070' AND `condition` = 'new' ORDER BY current_price
      - 查预算内的显卡型号：
        SELECT name, MIN(current_price) AS low
        FROM products WHERE category = 'gpu' AND `condition` = 'new'
        GROUP BY name HAVING low <= 5000 ORDER BY low
      - 取最低价对应的平台（不要 GROUP BY 后直接选 platform，那不代表最低价那一行）：
        SELECT p.name, p.platform, p.current_price
        FROM products p
        JOIN (SELECT name, MIN(current_price) AS low FROM products
              WHERE category = 'gpu' AND `condition` = 'new' GROUP BY name) m
          ON p.name = m.name AND p.current_price = m.low
        ORDER BY p.current_price
      - 查某型号一段时间的每天最低价：
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

    # 只放行单条只读语句；连接层还有只读限制，双重保险
    if not re.match(r"^\s*(select|with)\b", statement, re.I):
        return "只允许 SELECT 或 WITH 开头的查询，不能修改数据库。"
    if ";" in statement:
        return "一次只能执行一条 SQL，请去掉中间的分号。"

    if _DB_BACKEND == "sqlite" and not _SQLITE_PATH.exists():
        return f"数据库文件不存在：{_SQLITE_PATH}"

    try:
        columns, rows, latest_date = _execute(statement)
    except Exception as exc:
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
