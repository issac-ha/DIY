"""
硬件查询工具：Agent 调用这些工具来获取真实数据

注意：工具函数的 docstring 就是给 LLM 看的"说明书"，
LLM 根据 docstring 决定什么时候调用、传什么参数。
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from app.db.database import SessionLocal
from app.db.models import Product
from app.db.repository import get_platform_prices, get_price_history, get_digest, search_news
from app.tools.sql_tools import query_database

# 平台中文名
PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多", "douyin": "抖音"}

# 视频总结报告目录
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def _platform_cn(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform)


def _fmt_change(change: dict) -> str:
    """把单平台的涨跌字典格式化成 '（较前日 +23 元 / +0.33%）'"""
    if not change or change.get("change") is None:
        return ""
    amount = change["change"]
    pct = change.get("change_pct")
    if amount == 0:
        return "（较前日持平）"
    pct_text = f" / {pct:+.2f}%" if pct is not None else ""
    return f"（较前日 {amount:+.0f} 元{pct_text}）"


def _parse_tse(specs: str) -> Optional[int]:
    """从规格文本里解析 TSE 跑分，如 'TSE 跑分 10096'"""
    if not specs:
        return None
    m = re.search(r"(\d{3,6})", specs)
    return int(m.group(1)) if m else None


@tool
def get_product_info(name: str, condition: str = "new") -> str:
    """
    根据产品名称查询显卡/内存/硬盘在各平台的价格，并对比最低价和性价比。

    参数:
        name: 产品名称，例如 "RTX 5070"、"DDR5 32GB*2"、"PCIe 4.0 1TB"
        condition: 成色，"new" 全新（默认），"used" 二手（闲鱼成交价）

    返回: 各平台价格、今日最低价、性价比（显卡看 TSE 跑分/每千元，内存硬盘看每GB价格）
    """
    data = get_platform_prices(name, condition=condition)
    if not data:
        return f"没有找到名为 {name} 的产品"

    prices = data["prices"]
    label = "二手" if data["condition"] == "used" else "全新"
    lines = [f"产品：{data['name']}（{data['category']}，{label}）"]
    if data["brands"]:
        lines.append(f"品牌：{'、'.join(data['brands'])}")

    lines.append("各平台今日价格（括号内为较前一日涨跌）：")
    changes = data.get("changes", {})
    for platform, price in sorted(prices.items(), key=lambda x: x[1]):
        lines.append(f"  - {_platform_cn(platform)}：{price} 元{_fmt_change(changes.get(platform))}")

    lines.append(
        f"今日最低：{data['lowest_price']} 元（{_platform_cn(data['lowest_platform'])}）"
        f"{_fmt_change(changes.get(data['lowest_platform']))}"
    )

    # 性价比：TSE 跑分 / 最低价，按每千元折算
    db = SessionLocal()
    try:
        product = db.query(Product).filter(
            Product.name == data["name"],
            Product.condition == data["condition"],
        ).first()
        tse = _parse_tse(product.specs) if product else None
    finally:
        db.close()
    if tse and data["lowest_price"]:
        score = round(tse / data["lowest_price"] * 1000, 2)
        lines.append(f"TSE 跑分：{tse}，性价比：{score} 分/千元")

    return "\n".join(lines)


@tool
def get_price_trend(name: str, days: int = 7, platform: str = "",
                    condition: str = "new") -> str:
    """
    查询产品最近 N 天的价格趋势，计算涨跌幅度。

    参数:
        name: 产品名称，例如 "RTX 5070"
        days: 查询最近多少天的价格，默认 7 天
        platform: 可选，指定平台（jd/taobao/pdd/douyin/xianyu）。不填则每天取全网最低价
        condition: 成色，"new" 全新（默认），"used" 二手

    返回: 起始价、最新价、涨跌金额和百分比
    """
    histories = get_price_history(name, platform=platform or None, days=days * 4,
                                  condition=condition)
    if not histories:
        return f"没有找到 {name} 的价格历史"

    # 按天聚合：同一天多平台取最低价
    by_day = {}
    for h in histories:
        day = h["date"].strftime("%m-%d")
        if day not in by_day or h["price"] < by_day[day]:
            by_day[day] = h["price"]

    days_sorted = sorted(by_day)[-days:]
    if len(days_sorted) < 2:
        return f"{name} 价格数据不足，无法分析趋势"

    old_price = by_day[days_sorted[0]]
    new_price = by_day[days_sorted[-1]]
    diff = round(new_price - old_price, 2)
    percent = round(diff / old_price * 100, 2)
    trend = "上涨" if diff > 0 else "下跌" if diff < 0 else "持平"

    daily = ", ".join(f"{d}:{by_day[d]}" for d in days_sorted)
    scope = f"{_platform_cn(platform)}" if platform else "全网最低"
    return (
        f"{name} 最近 {len(days_sorted)} 天价格趋势（{scope}）：\n"
        f"起始价：{old_price} 元\n"
        f"最新价：{new_price} 元\n"
        f"涨跌：{diff} 元（{percent}%），整体{trend}\n"
        f"每日价格：{daily}"
    )


@tool
def list_products(category: Optional[str] = None, condition: str = "new") -> str:
    """
    列出所有产品及其全网最低价，可按分类和成色筛选。

    参数:
        category: 可选，产品分类，"gpu" 显卡、"memory" 内存、"ssd" 硬盘。不填则列出全部。
        condition: 成色，"new" 全新（默认），"used" 二手

    返回: 产品名称、最低价、最低价所在平台
    """
    db = SessionLocal()
    try:
        query = db.query(Product).filter(Product.condition == condition)
        if category:
            query = query.filter(Product.category == category)
        rows = query.all()
    finally:
        db.close()

    if not rows:
        return "没有找到产品"

    # 按型号聚合：同一型号在不同平台有多条记录
    grouped = {}
    for p in rows:
        if not p.current_price:
            continue
        grouped.setdefault(p.name, {})[p.platform] = p.current_price

    if not grouped:
        return "没有找到有价格的产品"

    lines = [f"共 {len(grouped)} 个产品："]
    for name in sorted(grouped):
        prices = grouped[name]
        platform = min(prices, key=prices.get)
        lines.append(f"- {name} | 最低 {prices[platform]} 元 @ {_platform_cn(platform)}")
    return "\n".join(lines)


@tool
def get_video_summary(keyword: str = "") -> str:
    """
    查询B站硬件资讯视频的 AI 总结报告（含视频画面中的价格信息）。

    参数:
        keyword: 可选，关键词过滤，如 "5070"、"内存"。不填返回最新报告

    返回: 最近的视频总结内容
    """
    if not REPORTS_DIR.exists():
        return "还没有生成视频总结报告，请先运行 python -m app.crawler.bilibili_pipeline"

    files = sorted(REPORTS_DIR.glob("bilibili_summary_*.md"), reverse=True)
    if not files:
        return "还没有生成视频总结报告"

    if keyword:
        matched = []
        for f in files:
            if keyword.lower() in f.read_text(encoding="utf-8").lower():
                matched.append(f)
        files = matched[:2]
    else:
        files = files[:1]

    if not files:
        return f"没有找到包含关键词 {keyword} 的视频总结"

    parts = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        parts.append(content[:2500])  # 截断，避免超出上下文
    return "\n\n".join(parts)


@tool
def get_market_digest(day: str = "") -> str:
    """
    查询某一天的硬件行情归纳，适合回答"今天行情怎么样""整体涨还是跌"这类问题。
    内容来自B站日报视频的旁白，包含大盘涨跌、板块表现和点名的型号价格。

    参数:
        day: 可选，日期格式 YYYY-MM-DD。不填返回最近一期

    返回: 当天的价格归纳和当期提到的新闻标题
    """
    target = None
    if day:
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return f"日期格式不对：{day}，应为 YYYY-MM-DD"

    digest = get_digest(target)
    if not digest:
        return f"没有找到 {day or '最近'} 的行情归纳，可能这一期还没采集"

    lines = [f"{digest['date']} 行情（{digest['title']}）"]
    if digest["market_summary"]:
        lines.append(digest["market_summary"])
    else:
        lines.append("这一期没有价格归纳")
    if digest["news"]:
        lines.append("")
        lines.append("当期新闻：")
        lines.extend(f"- {item['title']}" for item in digest["news"])
    return "\n".join(lines)


@tool
def search_hardware_news(keyword: str = "", days: int = 7) -> str:
    """
    检索最近的硬件行业新闻：新品发布、规格标准、厂商动作、供应链砍单、人事变动。
    价格涨跌不在这里，问价格请用 get_product_info 或 get_price_trend。

    参数:
        keyword: 可选，关键词，如 "DDR6"、"闪存"、"英伟达"。不填返回最近全部新闻
        days: 查询最近多少天，默认 7 天

    返回: 每条新闻的日期、标题和要点
    """
    items = search_news(keyword=keyword, days=days)
    if not items:
        scope = f"包含 {keyword} 的" if keyword else ""
        return f"最近 {days} 天没有找到{scope}硬件新闻"

    lines = [f"最近 {days} 天{'包含 ' + keyword + ' 的' if keyword else ''}硬件新闻（{len(items)} 条）："]
    for item in items:
        lines.append(f"- {item['date']}｜{item['title']}：{item['detail']}")
    return "\n".join(lines)


# 导出所有工具，供 Agent 使用
TOOLS = [query_database, get_product_info, get_price_trend, list_products,
         get_video_summary, get_market_digest, search_hardware_news]
