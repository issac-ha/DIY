"""
硬件查询工具：Agent 调用这些工具来获取真实数据

注意：工具函数的 docstring 就是给 LLM 看的"说明书"，
LLM 根据 docstring 决定什么时候调用、传什么参数。
"""
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from app.db.database import SessionLocal
from app.db.models import Product
from app.db.repository import get_platform_prices, get_price_history

# 平台中文名
PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多", "douyin": "抖音"}

# 视频总结报告目录
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def _platform_cn(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform)


def _parse_tse(specs: str) -> Optional[int]:
    """从规格文本里解析 TSE 跑分，如 'TSE 跑分 10096'"""
    if not specs:
        return None
    m = re.search(r"(\d{3,6})", specs)
    return int(m.group(1)) if m else None


@tool
def get_product_info(name: str) -> str:
    """
    根据产品名称查询显存在各平台的价格，并对比最低价和性价比。

    参数:
        name: 产品名称，例如 "RTX 5070"、"RX 9070 XT"

    返回: 各平台价格、今日最低价、性价比（TSE 跑分/每千元）
    """
    data = get_platform_prices(name)
    if not data:
        return f"没有找到名为 {name} 的产品"

    prices = data["prices"]
    lines = [f"产品：{data['name']}（{data['category']}）"]
    if data["brands"]:
        lines.append(f"品牌：{'、'.join(data['brands'])}")

    lines.append("各平台今日价格：")
    for platform, price in sorted(prices.items(), key=lambda x: x[1]):
        lines.append(f"  - {_platform_cn(platform)}：{price} 元")

    lines.append(
        f"今日最低：{data['lowest_price']} 元（{_platform_cn(data['lowest_platform'])}）"
    )

    # 性价比：TSE 跑分 / 最低价，按每千元折算
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.name == data["name"]).first()
        tse = _parse_tse(product.specs) if product else None
    finally:
        db.close()
    if tse and data["lowest_price"]:
        score = round(tse / data["lowest_price"] * 1000, 2)
        lines.append(f"TSE 跑分：{tse}，性价比：{score} 分/千元")

    return "\n".join(lines)


@tool
def get_price_trend(name: str, days: int = 7, platform: str = "") -> str:
    """
    查询产品最近 N 天的价格趋势，计算涨跌幅度。

    参数:
        name: 产品名称，例如 "RTX 5070"
        days: 查询最近多少天的价格，默认 7 天
        platform: 可选，指定平台（jd/taobao/pdd/douyin）。不填则每天取全网最低价

    返回: 起始价、最新价、涨跌金额和百分比
    """
    histories = get_price_history(name, platform=platform or None, days=days * 4)
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
def list_products(category: Optional[str] = None) -> str:
    """
    列出所有产品及其全网最低价，可按分类筛选。

    参数:
        category: 可选，产品分类，"gpu" 表示显卡，"memory" 表示内存。不填则列出全部。

    返回: 产品名称、最低价、最低价所在平台
    """
    db = SessionLocal()
    try:
        query = db.query(Product)
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


# 导出所有工具，供 Agent 使用
TOOLS = [get_product_info, get_price_trend, list_products, get_video_summary]
