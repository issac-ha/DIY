"""
硬件查询工具：Agent 调用这些工具来获取真实数据

注意：工具函数的 docstring 就是给 LLM 看的"说明书"，
LLM 根据 docstring 决定什么时候调用、传什么参数。
"""
from typing import Optional
from langchain_core.tools import tool

from app.db.database import SessionLocal
from app.db.models import Product, PriceHistory


@tool
def get_product_info(name: str) -> str:
    """
    根据产品名称查询硬件的当前价格和规格参数。

    参数:
        name: 产品名称，例如 "RTX 4060"、"DDR5 6000 16GB"

    返回: 产品的品牌、规格、当前价格；找不到则返回提示
    """
    db = SessionLocal()
    try:
        # 模糊匹配：产品名包含用户输入的关键词
        product = db.query(Product).filter(Product.name.contains(name)).first()
        if not product:
            return f"没有找到名为 {name} 的产品"
        return (
            f"产品：{product.name}\n"
            f"品牌：{product.brand}\n"
            f"分类：{product.category}\n"
            f"规格：{product.specs}\n"
            f"当前价格：{product.current_price} 元"
        )
    finally:
        db.close()


@tool
def get_price_trend(name: str, days: int = 7) -> str:
    """
    查询产品最近 N 天的价格趋势，计算涨跌幅度。

    参数:
        name: 产品名称，例如 "RTX 4060"
        days: 查询最近多少天的价格，默认 7 天

    返回: 最早价格、最新价格、涨跌金额和百分比
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.name.contains(name)).first()
        if not product:
            return f"没有找到名为 {name} 的产品"

        # 取最近 days 天的价格记录，按日期升序
        histories = (
            db.query(PriceHistory)
            .filter(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.date.desc())
            .limit(days)
            .all()
        )
        histories = list(reversed(histories))  # 转成升序：从早到晚

        if len(histories) < 2:
            return f"{product.name} 价格数据不足，无法分析趋势"

        old_price = histories[0].price
        new_price = histories[-1].price
        diff = round(new_price - old_price, 2)
        percent = round(diff / old_price * 100, 2)

        trend = "上涨" if diff > 0 else "下跌" if diff < 0 else "持平"
        # 拼接每天的价格，方便前端画图
        daily_prices = ", ".join(
            f"{h.date.strftime('%m-%d')}:{h.price}" for h in histories
        )

        return (
            f"{product.name} 最近 {days} 天价格趋势：\n"
            f"起始价：{old_price} 元\n"
            f"最新价：{new_price} 元\n"
            f"涨跌：{diff} 元（{percent}%），整体{trend}\n"
            f"每日价格：{daily_prices}"
        )
    finally:
        db.close()


@tool
def list_products(category: Optional[str] = None) -> str:
    """
    列出所有产品，可按分类筛选。

    参数:
        category: 可选，产品分类，"gpu" 表示显卡，"memory" 表示内存。不填则列出全部。

    返回: 产品名称和当前价格列表
    """
    db = SessionLocal()
    try:
        query = db.query(Product)
        if category:
            query = query.filter(Product.category == category)
        products = query.all()

        if not products:
            return "没有找到产品"

        lines = [f"共 {len(products)} 个产品："]
        for p in products:
            lines.append(f"- {p.name} | {p.brand} | {p.current_price} 元")
        return "\n".join(lines)
    finally:
        db.close()


# 导出所有工具，供 Agent 使用
TOOLS = [get_product_info, get_price_trend, list_products]
