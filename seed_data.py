"""
初始化数据库：用基准价格表（app/data/benchmark_prices.json）建表并写入数据

每个型号的每个平台各存一条 Product 记录，并生成过去 30 天的价格历史。
运行：python seed_data.py
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

from app.db.database import engine, SessionLocal, Base
from app.db.models import Product, PriceHistory

BENCHMARK_FILE = Path(__file__).resolve().parent / "app" / "data" / "benchmark_prices.json"

# 平台中文名，仅用于日志
PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多", "douyin": "抖音"}


def _build_specs(category: str, info: dict) -> str:
    """
    按品类生成规格文本

    显卡看 TSE 跑分（性能），CPU 看 R23 跑分（性能），内存/硬盘看每GB价格（性价比），
    三者不能混用，否则 get_product_info 会拿内存的每GB价格去算跑分性价比。
    """
    if category == "gpu":
        return f"TSE 跑分 {info.get('tse_score', '')}"
    if category == "cpu":
        return f"R23 跑分 {info.get('r23_score', '')}"
    return f"每GB {info.get('per_gb', '')} 元"


def load_products() -> list:
    """
    从基准价格表读取产品，展开成 (型号, 平台, 价格) 列表

    products 是全新价，used_products 是闲鱼二手成交价。
    两个区块结构相同，只有 condition 不同，所以循环同一段代码处理。
    """
    data = json.loads(BENCHMARK_FILE.read_text(encoding="utf-8"))
    items = []
    for section in ("products", "used_products"):
        for name, info in data.get(section, {}).items():
            category = info.get("category", "gpu")
            condition = info.get("condition", "new")
            for platform, price in info.get("prices", {}).items():
                if not price:  # 0 表示无货
                    continue
                items.append({
                    "name": name,
                    "platform": platform,
                    "price": float(price),
                    "category": category,
                    "condition": condition,
                    "brand": info.get("brand", ""),
                    "specs": _build_specs(category, info),
                    "lowest_shop": info.get("lowest_shop", ""),
                    "lowest_model": info.get("lowest_model", ""),
                })
    return items


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # 清空旧数据，方便重复运行
    db.query(PriceHistory).delete()
    db.query(Product).delete()
    db.commit()

    products = load_products()
    today = date.today()

    for data in products:
        product = Product(
            name=data["name"],
            category=data["category"],
            brand=data["brand"],
            specs=data["specs"],
            current_price=data["price"],
            platform=data["platform"],
            condition=data["condition"],
            lowest_shop=data["lowest_shop"],
            lowest_model=data["lowest_model"],
        )
        db.add(product)
        db.flush()

        # 过去 30 天历史：在当前价附近 ±3% 波动，最后一天对齐基准价
        # 相邻两天做差得到当天涨跌，第一天没有前一天可比，涨跌留空
        prev_price = None
        change = None
        change_pct = None
        for i in range(30, 0, -1):
            day = today - timedelta(days=i)
            if i == 1:
                price = round(data["price"], 2)
            else:
                price = round(data["price"] * (1 + random.uniform(-0.03, 0.03)), 2)
            if prev_price:
                change = round(price - prev_price, 2)
                change_pct = round(change / prev_price * 100, 2)
            else:
                change = None
                change_pct = None
            db.add(PriceHistory(
                product_id=product.id,
                price=price,
                change=change,
                change_pct=change_pct,
                date=day,
                platform=data["platform"],
            ))
            prev_price = price

        # 产品表保存最新一天的涨跌，列表页直接读
        product.change = change
        product.change_pct = change_pct

    db.commit()

    models = len({(p["name"], p["condition"]) for p in products})
    platforms = len({p["platform"] for p in products})
    by_kind = {}
    for p in products:
        by_kind.setdefault((p["category"], p["condition"]), set()).add(p["name"])
    labels = {"new": "全新", "used": "二手"}
    breakdown = "，".join(
        f"{cat}-{labels.get(cond, cond)} {len(names)}"
        for (cat, cond), names in by_kind.items()
    )
    print(f"初始化完成：{models} 个型号（{breakdown}），{len(products)} 条记录"
          f"（{platforms} 个平台），每条 30 天历史")
    db.close()


if __name__ == "__main__":
    seed()
